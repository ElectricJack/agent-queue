"""Pre-trusting a Codex session's project in ``config.toml`` (nimble-harbor-12)."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tomllib
from pathlib import Path

import pytest

from src.sessions.codex_trust import (
    codex_config_path,
    ensure_project_trusted,
    pretrust_codex_project,
    trust_root,
)


def _repo(path: Path) -> Path:
    (path / ".git").mkdir(parents=True)
    return path


def _trust(config: Path, root: Path) -> str | None:
    return tomllib.loads(config.read_text())["projects"][str(root.resolve())].get("trust_level")


class TestTrustRoot:
    def test_a_checkout_and_its_subdirectories_resolve_to_the_checkout(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        (repo / "src" / "pkg").mkdir(parents=True)
        assert trust_root(repo) == repo.resolve()
        assert trust_root(repo / "src" / "pkg") == repo.resolve()

    def test_a_directory_outside_git_is_its_own_root(self, tmp_path):
        assert trust_root(tmp_path) == tmp_path.resolve()

    def test_a_linked_worktree_resolves_to_the_main_checkout(self, tmp_path):
        # Codex 0.162 offers to trust <repo> for a session in a slot under it.
        repo = tmp_path / "repo"
        repo.mkdir()
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
        subprocess.run([*git, "init", "-q", str(repo)], check=True)
        subprocess.run([*git, "-C", str(repo), "commit", "-q", "--allow-empty", "-m", "i"],
                       check=True)
        slot = repo / ".aq" / "worktrees" / "slot-1"
        subprocess.run([*git, "-C", str(repo), "worktree", "add", "-q", "-b", "s1", str(slot)],
                       check=True)
        assert (slot / ".git").is_file()
        assert trust_root(slot) == repo.resolve()

    def test_a_relative_gitdir_is_followed(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        admin = repo / ".git" / "worktrees" / "w"
        admin.mkdir(parents=True)
        (admin / "commondir").write_text("../..\n")
        wt = tmp_path / "elsewhere" / "w"
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {os.path.relpath(admin, wt)}\n")
        assert trust_root(wt) == repo.resolve()

    def test_an_unreadable_git_file_keeps_the_worktree(self, tmp_path):
        wt = tmp_path / "w"
        wt.mkdir()
        (wt / ".git").write_text("not a gitdir line\n")
        assert trust_root(wt) == wt.resolve()


class TestConfigPath:
    def test_codex_home_wins(self, tmp_path):
        env = {"CODEX_HOME": str(tmp_path / "ch"), "HOME": str(tmp_path / "h")}
        assert codex_config_path(env) == tmp_path / "ch" / "config.toml"

    def test_home_is_the_fallback(self, tmp_path):
        assert codex_config_path({"HOME": str(tmp_path)}) == tmp_path / ".codex" / "config.toml"


class TestEnsureProjectTrusted:
    def test_a_missing_config_is_created_with_the_row(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "codex" / "config.toml"
        assert ensure_project_trusted(repo, config) == "added"
        assert _trust(config, repo) == "trusted"
        assert config.stat().st_mode & 0o777 == 0o600

    def test_existing_content_and_mode_are_preserved(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "config.toml"
        original = 'model = "gpt-5"\n\n[projects."/other"]\ntrust_level = "trusted"\n'
        config.write_text(original)
        config.chmod(0o644)
        assert ensure_project_trusted(repo, config) == "added"
        text = config.read_text()
        assert text.startswith(original)
        parsed = tomllib.loads(text)
        assert parsed["model"] == "gpt-5"
        assert parsed["projects"]["/other"]["trust_level"] == "trusted"
        assert _trust(config, repo) == "trusted"
        assert config.stat().st_mode & 0o777 == 0o644

    def test_a_file_without_a_trailing_newline_still_parses(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "config.toml"
        config.write_text('model = "gpt-5"')
        assert ensure_project_trusted(repo, config) == "added"
        assert tomllib.loads(config.read_text())["model"] == "gpt-5"
        assert _trust(config, repo) == "trusted"

    def test_a_second_call_is_a_no_op(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "config.toml"
        ensure_project_trusted(repo, config)
        before = config.read_text()
        assert ensure_project_trusted(repo / ".", config) == "present"
        assert config.read_text() == before

    def test_an_operator_untrusted_row_is_never_overridden(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "config.toml"
        original = f'[projects."{repo.resolve()}"]\ntrust_level = "untrusted"\n'
        config.write_text(original)
        assert ensure_project_trusted(repo, config) == "present"
        assert config.read_text() == original

    @pytest.mark.parametrize("text", [
        "this is = = not toml\n",
        'projects = { "/other" = { trust_level = "trusted" } }\n',
    ])
    def test_a_config_it_cannot_extend_is_left_byte_for_byte(self, tmp_path, text):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "config.toml"
        config.write_text(text)
        assert ensure_project_trusted(repo, config) == "skipped"
        assert config.read_text() == text

    def test_a_header_for_the_root_without_a_trust_level_is_skipped(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        config = tmp_path / "config.toml"
        text = f'[projects."{repo.resolve()}"]\nnote = "x"\n'
        config.write_text(text)
        assert ensure_project_trusted(repo, config) == "skipped"
        assert config.read_text() == text

    @pytest.mark.parametrize("name", ["agent.queue-2", 'quo"te', "back\\slash", "ünï code"])
    def test_awkward_paths_round_trip(self, tmp_path, name):
        # Dotted paths are why this edits the file instead of passing
        # ``-c projects."<path>".trust_level=...``.
        repo = _repo(tmp_path / name)
        config = tmp_path / "config.toml"
        assert ensure_project_trusted(repo, config) == "added"
        assert _trust(config, repo) == "trusted"

    def test_a_symlinked_config_is_updated_through_the_link(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        real = tmp_path / "dotfiles" / "config.toml"
        real.parent.mkdir()
        real.write_text('model = "gpt-5"\n')
        link = tmp_path / "config.toml"
        link.symlink_to(real)
        assert ensure_project_trusted(repo, link) == "added"
        assert link.is_symlink()
        assert _trust(real, repo) == "trusted"


class TestPretrust:
    async def test_honours_the_session_codex_home(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        home = tmp_path / "codex-home"
        assert await pretrust_codex_project(str(repo), {"CODEX_HOME": str(home)}) == "added"
        assert _trust(home / "config.toml", repo) == "trusted"

    async def test_concurrent_launches_write_one_row(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        env = {"CODEX_HOME": str(tmp_path / "ch")}
        results = await asyncio.gather(*(pretrust_codex_project(str(repo), env) for _ in range(8)))
        assert sorted(results) == ["added"] + ["present"] * 7
        text = (tmp_path / "ch" / "config.toml").read_text()
        assert text.count("[projects.") == 1

    async def test_an_unwritable_home_never_raises(self, tmp_path):
        repo = _repo(tmp_path / "repo")
        blocker = tmp_path / "file"
        blocker.write_text("")
        env = {"CODEX_HOME": str(blocker / "sub")}
        assert await pretrust_codex_project(str(repo), env) == "skipped"
