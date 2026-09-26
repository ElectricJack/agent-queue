from __future__ import annotations

import os

import pytest

from src.git.manager import GitManager
from src.test_selection.snapshot import (
    INCOMPLETE_REASONS,
    ChangedPath,
    ChangeSnapshot,
    hash_file,
    snapshot_fingerprint,
    take_snapshot,
)
from tests.selection_fixture_repo import build_fixture_repo, git


@pytest.fixture(autouse=True)
def _pg_backend():
    """Git only; never allocate a database."""


@pytest.fixture
def repo(tmp_path):
    return build_fixture_repo(tmp_path / "repo")


def _by_path(snapshot: ChangeSnapshot) -> dict[str, ChangedPath]:
    return {c.path: c for c in snapshot.changes}


async def test_a_clean_tree_is_a_complete_empty_snapshot(repo):
    snap = await take_snapshot(GitManager(), str(repo))
    assert snap.complete and snap.changes == () and snap.base_sha == snap.head_sha
    assert snap.fingerprint().startswith("sha256:")


async def test_dirty_staged_untracked_deleted_and_renamed_are_all_inventoried(repo):
    (repo / "src/pkg/a.py").write_text("def alpha():\n    return 10\n")  # unstaged
    (repo / "src/pkg/c.py").write_text("def gamma():\n    return 30\n")
    git(repo, "add", "src/pkg/c.py")  # staged
    (repo / "src/pkg/new.py").write_text("X = 1\n")  # untracked
    git(repo, "rm", "-q", "tests/test_c.py")  # deleted
    git(repo, "mv", "src/pkg/b.py", "src/pkg/d.py")  # renamed
    snap = await take_snapshot(GitManager(), str(repo))
    assert snap.complete
    changes = _by_path(snap)
    assert changes["src/pkg/a.py"].status == "modified"
    assert changes["src/pkg/a.py"].new_blob.startswith("sha256:")
    assert changes["src/pkg/a.py"].old_blob.startswith("git:")
    assert changes["src/pkg/c.py"].status == "modified"
    assert changes["src/pkg/new.py"].status == "untracked"
    assert changes["src/pkg/new.py"].old_blob is None
    assert changes["tests/test_c.py"].status == "deleted"
    assert changes["tests/test_c.py"].new_blob is None
    assert changes["src/pkg/d.py"].status == "renamed"
    assert changes["src/pkg/d.py"].old_path == "src/pkg/b.py"
    assert {"src/pkg/b.py", "tests/test_c.py", "src/pkg/d.py"} <= snap.paths
    assert "return 10" in changes["src/pkg/a.py"].excerpt
    assert changes["src/pkg/a.py"].hunks == ("def alpha():",)


async def test_committed_changes_since_the_merge_base_count_too(repo):
    git(repo, "checkout", "-q", "-b", "feature")
    (repo / "src/pkg/a.py").write_text("def alpha():\n    return 2\n")
    git(repo, "commit", "-qam", "change a")
    snap = await take_snapshot(GitManager(), str(repo))
    assert snap.complete and snap.base_sha != snap.head_sha
    assert _by_path(snap)["src/pkg/a.py"].status == "modified"


async def test_an_absent_base_ref_is_incomplete_and_never_fetched(repo):
    # The remote has the branch; only a fetch could make ``origin/nope`` resolve.
    git(repo.with_name(repo.name + ".origin.git"), "branch", "nope", "main")
    snap = await take_snapshot(GitManager(), str(repo), base_ref="origin/nope")
    assert (snap.complete, snap.incomplete_reason, snap.incomplete_detail) == (
        False,
        "unknown_base",
        "origin/nope",
    )
    assert snap.changes == () and snap.head_sha
    assert git(repo, "branch", "-r", "--list", "origin/nope") == ""


async def test_a_fresh_clone_without_origin_main_is_unknown_base(repo, tmp_path):
    lone = tmp_path / "lone"
    git(tmp_path, "clone", "-q", "--no-hardlinks", str(repo), str(lone))
    git(lone, "remote", "remove", "origin")
    snap = await take_snapshot(GitManager(), str(lone))
    assert snap.incomplete_reason == "unknown_base"
    assert snap.incomplete_detail == "origin/main"


async def test_a_base_ref_shaped_like_an_option_is_unknown_base(repo):
    snap = await take_snapshot(GitManager(), str(repo), base_ref="--all")
    assert (snap.incomplete_reason, snap.incomplete_detail) == ("unknown_base", "--all")


async def test_an_unreadable_changed_file_is_incomplete(repo):
    target = repo / "src/pkg/a.py"
    target.write_text("def alpha():\n    return 5\n")
    target.chmod(0)
    try:
        snap = await take_snapshot(GitManager(), str(repo))
    finally:
        target.chmod(0o644)
    assert (snap.complete, snap.incomplete_reason, snap.incomplete_detail) == (
        False,
        "unreadable_path",
        "src/pkg/a.py",
    )
    assert snap.changes == ()


async def test_a_symlink_escaping_the_workspace_is_incomplete(repo, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    os.symlink(outside, repo / "src/pkg/link.py")
    snap = await take_snapshot(GitManager(), str(repo))
    assert (snap.incomplete_reason, snap.incomplete_detail) == (
        "path_escapes_workspace",
        "src/pkg/link.py",
    )


async def test_a_symlink_inside_the_workspace_is_hashed_through(repo):
    os.symlink("a.py", repo / "src/pkg/alias.py")
    snap = await take_snapshot(GitManager(), str(repo))
    assert snap.complete
    assert _by_path(snap)["src/pkg/alias.py"].new_blob == hash_file(repo / "src/pkg/a.py")


async def test_an_unbounded_inventory_is_incomplete(repo, monkeypatch):
    monkeypatch.setattr("src.test_selection.snapshot.MAX_CHANGED_PATHS", 2)
    for name in ("x", "y", "z"):
        (repo / f"src/pkg/{name}.py").write_text("V = 1\n")
    snap = await take_snapshot(GitManager(), str(repo))
    assert snap.incomplete_reason == "inventory_unbounded" and snap.changes == ()
    assert snap.incomplete_detail == "3"


async def test_binary_and_oversize_files_hash_without_reading_everything(repo, monkeypatch):
    monkeypatch.setattr("src.test_selection.snapshot.MAX_HASH_BYTES", 64)
    (repo / "src/pkg/blob.bin").write_bytes(b"\x00\xff" * 100)
    (repo / "src/pkg/data.json").write_text("{" + '"k": 1,' * 40 + '"z": 0}')
    snap = await take_snapshot(GitManager(), str(repo))
    changes = _by_path(snap)
    assert snap.complete
    assert changes["src/pkg/blob.bin"].new_blob == "oversize"
    assert changes["src/pkg/blob.bin"].excerpt == ""
    assert changes["src/pkg/data.json"].new_blob == "oversize"
    assert changes["src/pkg/data.json"].excerpt == ""
    assert hash_file(repo / "src/pkg/a.py").startswith("sha256:")


async def test_a_binary_change_has_no_excerpt_or_hunks(repo):
    (repo / "src/pkg/blob.bin").write_bytes(b"\x00\x01" * 10)
    git(repo, "add", "src/pkg/blob.bin")
    git(repo, "commit", "-qm", "add a blob")
    (repo / "src/pkg/blob.bin").write_bytes(b"\x00\x02" * 10)
    (repo / "src/pkg/fresh.bin").write_bytes(b"\x00\x03" * 10)
    snap = await take_snapshot(GitManager(), str(repo))
    changes = _by_path(snap)
    for path in ("src/pkg/blob.bin", "src/pkg/fresh.bin"):
        assert changes[path].new_blob.startswith("sha256:")
        assert (changes[path].hunks, changes[path].excerpt) == ((), "")


async def test_excerpts_follow_renames_deletions_spaces_and_untracked_text(repo):
    git(repo, "mv", "src/pkg/b.py", "src/pkg/bee.py")
    (repo / "src/pkg/bee.py").write_text(
        "def beta():\n    from .a import alpha\n\n    return alpha() + 2\n"
    )
    git(repo, "rm", "-q", "src/pkg/c.py")
    (repo / "docs/user guide.md").write_text("# guide\n")
    git(repo, "add", "docs/user guide.md")
    git(repo, "commit", "-qm", "spaced")
    (repo / "docs/user guide.md").write_text("# guide\n\nmore\n")
    (repo / "src/pkg/fresh.py").write_text("\n".join(f"LINE_{n} = {n}" for n in range(50)))
    snap = await take_snapshot(GitManager(), str(repo), excerpt_lines=5)
    changes = _by_path(snap)
    assert changes["src/pkg/bee.py"].status == "renamed"
    assert "return alpha() + 2" in changes["src/pkg/bee.py"].excerpt
    assert changes["src/pkg/bee.py"].hunks == ("def beta():",)
    assert "-    return 3" in changes["src/pkg/c.py"].excerpt
    assert "+more" in changes["docs/user guide.md"].excerpt
    assert changes["src/pkg/fresh.py"].excerpt.splitlines() == [f"LINE_{n} = {n}" for n in range(5)]
    assert all(len(c.excerpt.splitlines()) <= 5 for c in snap.changes)


async def test_no_excerpt_is_read_when_none_is_asked_for(repo):
    (repo / "src/pkg/a.py").write_text("def alpha():\n    return 10\n")
    (repo / "src/pkg/new.py").write_text("X = 1\n")
    snap = await take_snapshot(GitManager(), str(repo), excerpt_lines=0)
    assert snap.complete and len(snap.changes) == 2
    assert all((c.hunks, c.excerpt) == ((), "") for c in snap.changes)


async def test_a_repository_without_a_commit_is_a_git_error(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q", "-b", "main")
    snap = await take_snapshot(GitManager(), str(empty))
    assert (snap.complete, snap.incomplete_reason, snap.head_sha) == (False, "git_error", "")


async def test_a_workspace_below_the_repository_top_level_is_a_git_error(repo):
    snap = await take_snapshot(GitManager(), str(repo / "src"))
    assert snap.incomplete_reason == "git_error"
    assert snap.incomplete_reason in INCOMPLETE_REASONS


async def test_the_fingerprint_changes_with_the_tree_and_the_cheap_recheck_agrees(repo):
    before = await snapshot_fingerprint(GitManager(), str(repo), base_ref="origin/main")
    assert before == (await take_snapshot(GitManager(), str(repo))).fingerprint()
    (repo / "src/pkg/a.py").write_text("def alpha():\n    return 7\n")
    after = await snapshot_fingerprint(GitManager(), str(repo), base_ref="origin/main")
    assert after != before
    git(repo, "checkout", "--", "src/pkg/a.py")
    assert await snapshot_fingerprint(GitManager(), str(repo), base_ref="origin/main") == before


async def test_the_fingerprint_sees_an_edit_to_an_already_dirty_file(repo):
    (repo / "src/pkg/new.py").write_text("X = 1\n")
    first = await snapshot_fingerprint(GitManager(), str(repo), base_ref="origin/main")
    (repo / "src/pkg/new.py").write_text("X = 2\n")
    assert await snapshot_fingerprint(GitManager(), str(repo), base_ref="origin/main") != first


async def test_an_incomplete_snapshot_never_matches_a_complete_fingerprint(repo, monkeypatch):
    clean = await snapshot_fingerprint(GitManager(), str(repo), base_ref="origin/main")
    monkeypatch.setattr("src.test_selection.snapshot.MAX_CHANGED_PATHS", 0)
    (repo / "src/pkg/new.py").write_text("X = 1\n")
    unbounded = await take_snapshot(GitManager(), str(repo))
    assert not unbounded.complete and unbounded.changes == ()
    assert unbounded.fingerprint() != clean


async def test_the_snapshot_records_the_clock_and_the_base_ref(repo):
    snap = await take_snapshot(GitManager(), str(repo), clock=lambda: 1234.5)
    assert (snap.taken_at, snap.base_ref, snap.workspace) == (1234.5, "origin/main", str(repo))


def test_malformed_raw_diff_output_is_a_git_error():
    from src.test_selection.snapshot import _Incomplete, _parse_raw

    good = ":100644 100644 " + "1" * 40 + " " + "0" * 40 + " M\0src/pkg/a.py\0"
    assert [c.path for c in _parse_raw(good)] == ["src/pkg/a.py"]
    assert _parse_raw("") == []
    for bad in (good[:-1], good + "\0" + good, ":100644 100644 x y R100\0only-one\0"):
        with pytest.raises(_Incomplete) as caught:
            _parse_raw(bad)
        assert (caught.value.reason, caught.value.detail) == ("git_error", "diff")
