"""Tests for ``src.projects.paths`` — root-relative path validation and safe listing.

Project-onboarding design §3.3 (root-relative capabilities), §5.1 (browse
contract) and §7 (security). Every case builds its own tree under ``tmp_path``;
nothing here touches the config module or the database.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from src.projects.paths import (
    DirectoryEntry,
    DirectoryListing,
    ProjectPathCode,
    ProjectPathError,
    ResolvedPath,
    invalid_component_reason,
    is_git_worktree_root,
    list_directory,
    validate_relative_path,
)

# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A project root with a nested plain directory tree and one file."""
    r = tmp_path / "root"
    (r / "a" / "b" / "c").mkdir(parents=True)
    (r / "a" / "file.txt").write_text("x")
    return r


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """A directory that is a sibling of the root, i.e. never beneath it."""
    o = tmp_path / "outside"
    (o / "secret").mkdir(parents=True)
    return o


def _make_git_dir_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir()
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return path


def _make_gitdir_file_repo(path: Path, target: str = "/somewhere/.git/worktrees/x") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").write_text(f"gitdir: {target}\n")
    return path


def _codes(exc_info: pytest.ExceptionInfo[ProjectPathError]) -> ProjectPathCode:
    return exc_info.value.code


# --------------------------------------------------------------------------
# validate_relative_path — acceptance
# --------------------------------------------------------------------------


class TestValidateAccepts:
    def test_empty_relative_is_the_root(self, root: Path) -> None:
        resolved = validate_relative_path(root, "")
        assert isinstance(resolved, ResolvedPath)
        assert resolved.relative == ""
        assert resolved.path == root.resolve()
        assert resolved.real_path == root.resolve()
        assert resolved.exists is True
        assert resolved.is_dir is True

    def test_dot_is_the_root(self, root: Path) -> None:
        assert validate_relative_path(root, ".").relative == ""

    def test_plain_nested_directory(self, root: Path) -> None:
        resolved = validate_relative_path(root, "a/b/c")
        assert resolved.relative == "a/b/c"
        assert resolved.path == root.resolve() / "a" / "b" / "c"
        assert resolved.real_path == (root / "a" / "b" / "c").resolve()
        assert resolved.is_dir is True

    def test_missing_child_is_allowed_without_require_directory(self, root: Path) -> None:
        # Mutation destinations (init/clone) must not exist yet; validation
        # alone therefore reports existence rather than demanding it.
        resolved = validate_relative_path(root, "a/new-repo")
        assert resolved.exists is False
        assert resolved.is_dir is False
        assert resolved.real_path == root.resolve() / "a" / "new-repo"

    def test_file_child_is_reported_as_not_a_directory(self, root: Path) -> None:
        resolved = validate_relative_path(root, "a/file.txt")
        assert resolved.exists is True
        assert resolved.is_dir is False

    def test_symlink_alias_inside_root_is_accepted(self, root: Path) -> None:
        (root / "alias").symlink_to(root / "a" / "b", target_is_directory=True)
        resolved = validate_relative_path(root, "alias/c")
        assert resolved.relative == "alias/c"
        assert resolved.real_path == (root / "a" / "b" / "c").resolve()
        assert resolved.is_dir is True

    def test_root_given_as_symlink_resolves_to_real_root(self, root: Path, tmp_path: Path) -> None:
        link = tmp_path / "root-link"
        link.symlink_to(root, target_is_directory=True)
        resolved = validate_relative_path(link, "a")
        assert resolved.root == root.resolve()
        assert resolved.real_path == (root / "a").resolve()

    def test_root_given_as_relative_path_is_resolved(self, root: Path, monkeypatch) -> None:
        monkeypatch.chdir(root.parent)
        resolved = validate_relative_path(Path("root"), "a")
        assert resolved.root == root.resolve()

    def test_dot_aq_itself_is_browsable(self, root: Path) -> None:
        (root / ".aq" / "worktrees" / "slot-1").mkdir(parents=True)
        assert validate_relative_path(root, ".aq").is_dir is True


# --------------------------------------------------------------------------
# validate_relative_path — rejections
# --------------------------------------------------------------------------


class TestValidateRejects:
    @pytest.mark.parametrize(
        "rel",
        ["..", "../x", "a/..", "a/../b", "a/b/../../..", "../../etc/passwd"],
    )
    def test_dot_dot_components(self, root: Path, rel: str) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE
        assert exc.value.relative_path == rel

    @pytest.mark.parametrize("rel", ["/", "/etc", "/etc/passwd", "//x"])
    def test_absolute_posix_child(self, root: Path, rel: str) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    @pytest.mark.parametrize("rel", ["C:/x", "C:\\x", "c:", "\\\\server\\share", "\\x"])
    def test_absolute_windows_child(self, root: Path, rel: str) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_absolute_child_equal_to_root_is_still_rejected(self, root: Path) -> None:
        # Even an absolute path that would land inside the root is not a
        # root-relative capability (§3.3): the dashboard never sends one.
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, str(root / "a"))
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    @pytest.mark.parametrize("rel", ["a\0b", "\0", "a/\0/b", "a\0"])
    def test_nul_bytes(self, root: Path, rel: str) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    @pytest.mark.parametrize("rel", ["a//b", "a/", "/a/", "a/./b", "./a", "a/."])
    def test_empty_and_dot_components(self, root: Path, rel: str) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    @pytest.mark.parametrize("rel", ["a\nb", "a\tb", "a\x7fb"])
    def test_control_characters(self, root: Path, rel: str) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_non_string_relative_is_a_type_error(self, root: Path) -> None:
        with pytest.raises(TypeError):
            validate_relative_path(root, Path("a"))  # type: ignore[arg-type]

    def test_symlink_whose_real_target_is_outside_root(self, root: Path, outside: Path) -> None:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "escape")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_child_of_escaping_symlink(self, root: Path, outside: Path) -> None:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "escape/secret")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_missing_child_beneath_escaping_symlink(self, root: Path, outside: Path) -> None:
        # The existing prefix resolves outside the root even though the leaf
        # does not exist yet: a create there would land outside the root.
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "escape/does-not-exist")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_sibling_with_root_as_string_prefix_is_not_inside(self, tmp_path: Path) -> None:
        # ``/tmp/root`` vs ``/tmp/root-evil``: string-prefix containment would
        # accept this; component-aware containment must not.
        root = tmp_path / "root"
        root.mkdir()
        evil = tmp_path / "root-evil"
        evil.mkdir()
        (root / "link").symlink_to(evil, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "link")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_symlink_to_root_parent(self, root: Path) -> None:
        (root / "up").symlink_to(root.parent, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "up")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE
        # ...and going back down through it to a real child is still an alias
        # of the parent, not a root-relative path.
        with pytest.raises(ProjectPathError) as exc2:
            validate_relative_path(root, "up/outside")
        assert _codes(exc2) is ProjectPathCode.ROOT_ESCAPE

    def test_symlink_back_into_root_via_parent_is_accepted(self, root: Path) -> None:
        # ``root/up -> root/..``; ``up/root/a`` resolves to ``root/a`` which
        # really is beneath the root.  Containment is about the real target.
        (root / "up").symlink_to(root.parent, target_is_directory=True)
        resolved = validate_relative_path(root, f"up/{root.name}/a")
        assert resolved.real_path == (root / "a").resolve()

    @pytest.mark.parametrize(
        "rel",
        [".aq/worktrees", ".aq/worktrees/slot-1", ".aq/worktrees/slot-1/src"],
    )
    def test_managed_worktrees_traversal(self, root: Path, rel: str) -> None:
        (root / ".aq" / "worktrees" / "slot-1" / "src").mkdir(parents=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, rel)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_managed_worktrees_traversal_is_rejected_even_when_absent(self, root: Path) -> None:
        # A create destination under ``.aq/worktrees`` is rejected lexically,
        # before any filesystem lookup.
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, ".aq/worktrees/new")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_managed_worktrees_alias_via_symlink(self, root: Path) -> None:
        slot = root / ".aq" / "worktrees" / "slot-1"
        slot.mkdir(parents=True)
        (root / "wt").symlink_to(slot, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "wt")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_managed_worktrees_alias_deeper_in_tree(self, root: Path) -> None:
        # ``.aq/worktrees`` of a *nested* repository is just as off-limits.
        slot = root / "a" / ".aq" / "worktrees" / "slot-3"
        slot.mkdir(parents=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "a/.aq/worktrees/slot-3")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_lookalike_worktrees_directory_is_fine(self, root: Path) -> None:
        # Only the exact ``.aq/worktrees`` pair is managed; ``worktrees`` on
        # its own or ``.aq/worktrees-old`` are ordinary directories.
        (root / "worktrees").mkdir()
        (root / ".aq" / "worktrees-old").mkdir(parents=True)
        assert validate_relative_path(root, "worktrees").is_dir is True
        assert validate_relative_path(root, ".aq/worktrees-old").is_dir is True

    def test_require_directory_missing(self, root: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "a/nope", require_directory=True)
        assert _codes(exc) is ProjectPathCode.NOT_FOUND

    def test_require_directory_on_file(self, root: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "a/file.txt", require_directory=True)
        assert _codes(exc) is ProjectPathCode.NOT_DIRECTORY

    def test_require_directory_on_dangling_symlink(self, root: Path) -> None:
        (root / "dangling").symlink_to(root / "gone")
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "dangling", require_directory=True)
        assert _codes(exc) is ProjectPathCode.NOT_FOUND


class TestRootUnavailable:
    def test_missing_root(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(tmp_path / "nope", "a")
        assert _codes(exc) is ProjectPathCode.ROOT_UNAVAILABLE

    def test_root_is_a_file(self, tmp_path: Path) -> None:
        f = tmp_path / "file"
        f.write_text("x")
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(f, "")
        assert _codes(exc) is ProjectPathCode.ROOT_UNAVAILABLE

    def test_root_inside_managed_worktrees(self, tmp_path: Path) -> None:
        slot = tmp_path / "repo" / ".aq" / "worktrees" / "slot-2"
        slot.mkdir(parents=True)
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(slot, "")
        assert _codes(exc) is ProjectPathCode.ROOT_UNAVAILABLE

    def test_root_unavailable_is_checked_before_the_relative_path(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(tmp_path / "nope", "../x")
        assert _codes(exc) is ProjectPathCode.ROOT_UNAVAILABLE


# --------------------------------------------------------------------------
# error type
# --------------------------------------------------------------------------


class TestProjectPathError:
    def test_is_a_value_error_with_code_and_relative_path(self, root: Path) -> None:
        with pytest.raises(ValueError) as exc:
            validate_relative_path(root, "../x")
        err = exc.value
        assert isinstance(err, ProjectPathError)
        assert err.code == "root_escape"
        assert err.relative_path == "../x"
        assert "root_escape" in str(err)

    def test_codes_are_the_contract_strings(self) -> None:
        assert {c.value for c in ProjectPathCode} == {
            "not_found",
            "not_directory",
            "root_escape",
            "root_unavailable",
        }

    def test_to_dict_is_json_friendly(self, root: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            validate_relative_path(root, "nope", require_directory=True)
        payload = exc.value.to_dict()
        assert payload["code"] == "not_found"
        assert payload["relative_path"] == "nope"
        assert isinstance(payload["message"], str)


# --------------------------------------------------------------------------
# list_directory
# --------------------------------------------------------------------------


class TestListDirectory:
    def test_root_listing_is_name_ordered_and_hides_dotfiles(self, root: Path) -> None:
        for name in ("zeta", "Alpha", "mid", ".hidden", ".git-ish"):
            (root / name).mkdir()
        (root / ".dotfile").write_text("")
        listing = list_directory(root, "")
        assert isinstance(listing, DirectoryListing)
        assert listing.relative == ""
        assert listing.truncated is False
        assert [e.name for e in listing.entries] == sorted(["a", "zeta", "Alpha", "mid"])
        assert all(isinstance(e, DirectoryEntry) for e in listing.entries)

    def test_include_hidden(self, root: Path) -> None:
        (root / ".hidden").mkdir()
        (root / ".dotfile").write_text("")
        names = [e.name for e in list_directory(root, "", include_hidden=True).entries]
        assert names == [".dotfile", ".hidden", "a"]

    def test_entries_carry_root_relative_paths(self, root: Path) -> None:
        listing = list_directory(root, "a")
        assert listing.relative == "a"
        by_name = {e.name: e for e in listing.entries}
        assert by_name["b"].relative_path == "a/b"
        assert by_name["b"].is_dir is True
        assert by_name["file.txt"].relative_path == "a/file.txt"
        assert by_name["file.txt"].is_dir is False
        assert by_name["file.txt"].is_git_repo is False
        assert by_name["file.txt"].selectable is False

    def test_root_entries_have_bare_names_as_relative_paths(self, root: Path) -> None:
        listing = list_directory(root, "")
        assert [e.relative_path for e in listing.entries] == ["a"]

    def test_dot_relative_normalises_to_root(self, root: Path) -> None:
        assert list_directory(root, ".").relative == ""

    def test_git_dir_repo_is_selectable(self, root: Path) -> None:
        _make_git_dir_repo(root / "repo")
        entry = {e.name: e for e in list_directory(root, "").entries}["repo"]
        assert entry.is_dir is True
        assert entry.is_git_repo is True
        assert entry.selectable is True

    def test_gitdir_file_repo_is_selectable(self, root: Path) -> None:
        _make_gitdir_file_repo(root / "linked")
        entry = {e.name: e for e in list_directory(root, "").entries}["linked"]
        assert entry.is_git_repo is True
        assert entry.selectable is True

    def test_git_dir_without_head_is_not_a_repo(self, root: Path) -> None:
        (root / "broken" / ".git").mkdir(parents=True)
        entry = {e.name: e for e in list_directory(root, "").entries}["broken"]
        assert entry.is_dir is True
        assert entry.is_git_repo is False
        assert entry.selectable is False

    def test_git_file_without_gitdir_prefix_is_not_a_repo(self, root: Path) -> None:
        (root / "odd").mkdir()
        (root / "odd" / ".git").write_text("not a pointer\n")
        entry = {e.name: e for e in list_directory(root, "").entries}["odd"]
        assert entry.is_git_repo is False
        assert entry.selectable is False

    def test_plain_directory_is_not_selectable(self, root: Path) -> None:
        entry = {e.name: e for e in list_directory(root, "").entries}["a"]
        assert entry.is_dir is True
        assert entry.is_git_repo is False
        assert entry.selectable is False

    def test_symlink_alias_inside_root_is_a_directory(self, root: Path) -> None:
        _make_git_dir_repo(root / "repo")
        (root / "alias").symlink_to(root / "repo", target_is_directory=True)
        entry = {e.name: e for e in list_directory(root, "").entries}["alias"]
        assert entry.is_dir is True
        assert entry.is_git_repo is True
        assert entry.selectable is True
        # ...and its children are browsable through the alias.
        assert list_directory(root, "alias", include_hidden=True).entries[0].name == ".git"

    def test_symlink_escaping_root_is_listed_as_not_a_directory(
        self, root: Path, outside: Path
    ) -> None:
        _make_git_dir_repo(outside)
        (root / "escape").symlink_to(outside, target_is_directory=True)
        entry = {e.name: e for e in list_directory(root, "").entries}["escape"]
        assert entry.is_dir is False
        assert entry.is_git_repo is False
        assert entry.selectable is False

    def test_symlink_escaping_root_is_not_descended(self, root: Path, outside: Path) -> None:
        (root / "escape").symlink_to(outside, target_is_directory=True)
        with pytest.raises(ProjectPathError) as exc:
            list_directory(root, "escape")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE
        with pytest.raises(ProjectPathError) as exc2:
            list_directory(root, "escape/secret")
        assert _codes(exc2) is ProjectPathCode.ROOT_ESCAPE

    def test_dangling_symlink_is_listed_as_not_a_directory(self, root: Path) -> None:
        (root / "dangling").symlink_to(root / "gone")
        entry = {e.name: e for e in list_directory(root, "").entries}["dangling"]
        assert entry.is_dir is False
        assert entry.selectable is False

    def test_managed_worktrees_are_not_shown_as_directories(self, root: Path) -> None:
        (root / ".aq" / "worktrees" / "slot-1").mkdir(parents=True)
        (root / ".aq" / "other").mkdir()
        entries = {e.name: e for e in list_directory(root, ".aq", include_hidden=True).entries}
        assert entries["other"].is_dir is True
        assert entries["worktrees"].is_dir is False
        assert entries["worktrees"].selectable is False

    def test_managed_worktrees_symlink_alias_is_not_a_directory(self, root: Path) -> None:
        slot = _make_git_dir_repo(root / ".aq" / "worktrees" / "slot-1")
        (root / "wt").symlink_to(slot, target_is_directory=True)
        entry = {e.name: e for e in list_directory(root, "").entries}["wt"]
        assert entry.is_dir is False
        assert entry.is_git_repo is False
        assert entry.selectable is False

    def test_managed_worktrees_cannot_be_browsed(self, root: Path) -> None:
        (root / ".aq" / "worktrees" / "slot-1").mkdir(parents=True)
        with pytest.raises(ProjectPathError) as exc:
            list_directory(root, ".aq/worktrees", include_hidden=True)
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_limit_truncates_after_sorting(self, root: Path) -> None:
        for name in ("d", "c", "b", "e"):
            (root / name).mkdir()
        listing = list_directory(root, "", limit=3)
        assert [e.name for e in listing.entries] == ["a", "b", "c"]
        assert listing.truncated is True

    def test_limit_equal_to_count_is_not_truncated(self, root: Path) -> None:
        (root / "b").mkdir()
        listing = list_directory(root, "", limit=2)
        assert [e.name for e in listing.entries] == ["a", "b"]
        assert listing.truncated is False

    def test_hidden_entries_do_not_count_against_the_limit(self, root: Path) -> None:
        for name in (".h1", ".h2", ".h3"):
            (root / name).mkdir()
        (root / "b").mkdir()
        listing = list_directory(root, "", limit=2)
        assert [e.name for e in listing.entries] == ["a", "b"]
        assert listing.truncated is False

    @pytest.mark.parametrize("limit", [0, -1])
    def test_limit_must_be_positive(self, root: Path, limit: int) -> None:
        with pytest.raises(ValueError):
            list_directory(root, "", limit=limit)

    def test_not_found(self, root: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            list_directory(root, "a/nope")
        assert _codes(exc) is ProjectPathCode.NOT_FOUND

    def test_not_directory(self, root: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            list_directory(root, "a/file.txt")
        assert _codes(exc) is ProjectPathCode.NOT_DIRECTORY

    def test_root_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            list_directory(tmp_path / "nope", "")
        assert _codes(exc) is ProjectPathCode.ROOT_UNAVAILABLE

    def test_escape_in_relative_path(self, root: Path) -> None:
        with pytest.raises(ProjectPathError) as exc:
            list_directory(root, "../")
        assert _codes(exc) is ProjectPathCode.ROOT_ESCAPE

    def test_listing_never_returns_file_contents(self, root: Path) -> None:
        (root / "a" / "file.txt").write_text("SECRET-CONTENT")
        listing = list_directory(root, "a")
        assert "SECRET-CONTENT" not in repr(listing)
        assert set(DirectoryEntry.__dataclass_fields__) == {
            "name",
            "relative_path",
            "is_dir",
            "is_git_repo",
            "selectable",
        }

    def test_to_dict_shapes(self, root: Path) -> None:
        _make_git_dir_repo(root / "repo")
        payload = list_directory(root, "").to_dict()
        assert payload["relative"] == ""
        assert payload["truncated"] is False
        assert payload["entries"][1] == {
            "name": "repo",
            "relative_path": "repo",
            "is_dir": True,
            "is_git_repo": True,
            "selectable": True,
        }


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class TestIsGitWorktreeRoot:
    def test_git_dir_with_head(self, tmp_path: Path) -> None:
        assert is_git_worktree_root(_make_git_dir_repo(tmp_path / "r")) is True

    def test_gitdir_file(self, tmp_path: Path) -> None:
        assert is_git_worktree_root(_make_gitdir_file_repo(tmp_path / "r")) is True

    def test_missing_git(self, tmp_path: Path) -> None:
        assert is_git_worktree_root(tmp_path) is False

    def test_not_a_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "f"
        f.write_text("")
        assert is_git_worktree_root(f) is False

    def test_bare_git_dir_is_not_a_worktree_root(self, tmp_path: Path) -> None:
        # ``repo.git/HEAD`` exists but ``repo.git/.git`` does not: a bare
        # repository is not a worktree and cannot be linked as one.
        bare = tmp_path / "repo.git"
        bare.mkdir()
        (bare / "HEAD").write_text("ref: refs/heads/main\n")
        assert is_git_worktree_root(bare) is False


class TestInvalidComponentReason:
    def test_posix_accepts_odd_but_legal_names(self) -> None:
        assert invalid_component_reason("a:b<c>d|e?f*g", windows=False) is None
        assert invalid_component_reason("trailing. ", windows=False) is None

    @pytest.mark.parametrize("name", ["", ".", ".."])
    def test_structural_components(self, name: str) -> None:
        assert invalid_component_reason(name, windows=False) is not None

    @pytest.mark.parametrize("name", ["a\0b", "a\nb", "a\x1fb", "a\x7fb"])
    def test_control_characters(self, name: str) -> None:
        assert invalid_component_reason(name, windows=False) is not None
        assert invalid_component_reason(name, windows=True) is not None

    @pytest.mark.parametrize(
        "name",
        [
            "a<b",
            "a>b",
            "a:b",
            'a"b',
            "a|b",
            "a?b",
            "a*b",
            "a\\b",
            "CON",
            "con.txt",
            "LPT1",
            "x.",
            "x ",
        ],
    )
    def test_windows_invalid_names(self, name: str) -> None:
        assert invalid_component_reason(name, windows=True) is not None

    def test_windows_accepts_ordinary_names(self) -> None:
        assert invalid_component_reason("Project Alpha_2", windows=True) is None
        assert invalid_component_reason("console", windows=True) is None


# --------------------------------------------------------------------------
# isolation: no config / database import
# --------------------------------------------------------------------------


def test_module_does_not_import_config_or_database() -> None:
    code = (
        "import sys\n"
        "import src.projects.paths\n"
        "bad = sorted(m for m in sys.modules if m == 'src.config' "
        "or m.startswith('src.database') or m.startswith('sqlalchemy'))\n"
        "print(','.join(bad))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert out.stdout.strip() == "", out.stdout
