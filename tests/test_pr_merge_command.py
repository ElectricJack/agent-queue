"""Tests for pr_merge command (dv2 Phase 2, Task 1).

Tests GitManager.amerge_pr and CommandHandler._cmd_pr_merge.
"""

from __future__ import annotations

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
    base_oid: str = "a" * 40,
    head_oid: str = "b" * 40,
    changed_files: object = 1,
) -> str:
    import json

    payload = {
        "baseRefName": "main",
        "baseRefOid": base_oid,
        "headRefName": "feature/guard",
        "headRefOid": head_oid,
        "baseRepository": {"nameWithOwner": "org/repo"},
    }
    if changed_files is not None:
        payload["changedFiles"] = changed_files
    return json.dumps(payload)


def _clean_paths(count: int) -> str:
    """``count`` distinct non-reserved paths, one per line, as gh prints them."""
    return "".join(f".a/{i:05d}\n" for i in range(count))


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
        if cmd[:3] == ["gh", "pr", "view"]:
            return MagicMock(returncode=0, stdout=_pr_identity_payload(changed_files=2), stderr="")
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
        if cmd[:3] == ["gh", "pr", "view"]:
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


def _gm_with_pr(monkeypatch, *, changed_files: object, listed: str, views: int = 2):
    """A GitManager whose gh reports ``changed_files`` and lists ``listed``."""
    from src.git.manager import GitManager

    gm = GitManager()
    snapshots = iter([_pr_identity_payload(changed_files=changed_files)] * views)

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if cmd[:3] == ["gh", "pr", "view"]:
            return MagicMock(returncode=0, stdout=next(snapshots), stderr="")
        assert cmd[:4] == ["gh", "api", "--paginate", "repos/org/repo/pulls/42/files"]
        return MagicMock(returncode=0, stdout=listed, stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    return gm


_PR_URL = "https://github.com/org/repo/pull/42"
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
    fields: list[str] = []

    async def fake_arun_subprocess(cmd, cwd, timeout):
        assert cmd[:3] == ["gh", "pr", "view"]
        fields.append(cmd[cmd.index("--json") + 1])
        return MagicMock(returncode=0, stdout=_pr_identity_payload(changed_files=7), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    identity = await gm.aget_pr_identity("/some/checkout", _PR_URL)

    assert identity.changed_files == 7
    # One ``gh pr view`` call carries the OIDs and the count together, so the
    # count belongs to the same base/head pair the listing is checked against.
    assert len(fields) == 1
    assert set(fields[0].split(",")) >= {"baseRefOid", "headRefOid", "changedFiles"}


@pytest.mark.asyncio
async def test_pr_identity_reads_the_rest_changed_files_name_too(monkeypatch):
    # The REST ``pulls/{n}`` resource spells the same fact ``changed_files``;
    # the identity must read it whichever endpoint produced the snapshot.
    import json

    from src.git.manager import GitManager

    gm = GitManager()
    payload = json.loads(_pr_identity_payload(changed_files=None))
    payload["changed_files"] = 5

    async def fake_arun_subprocess(cmd, cwd, timeout):
        return MagicMock(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(gm, "_arun_subprocess", fake_arun_subprocess)
    identity = await gm.aget_pr_identity("/some/checkout", _PR_URL)
    assert identity.changed_files == 5


@pytest.mark.asyncio
async def test_pr_validation_detects_a_changed_file_count_between_snapshots(monkeypatch):
    from src.git.manager import GitManager

    gm = GitManager()
    views = iter([_pr_identity_payload(changed_files=1), _pr_identity_payload(changed_files=2)])

    async def fake_arun_subprocess(cmd, cwd, timeout):
        if cmd[:3] == ["gh", "pr", "view"]:
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
        if cmd[:3] == ["gh", "pr", "view"]:
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
