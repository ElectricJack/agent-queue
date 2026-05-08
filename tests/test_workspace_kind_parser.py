"""WorkspaceKind markdown frontmatter parser. See spec §4.1."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from src.profiles.workspace_kind_parser import parse_workspace_kind_file


def test_parse_full_frontmatter(tmp_path: Path):
    md = tmp_path / "game-repo.md"
    md.write_text(
        textwrap.dedent(
            """
            ---
            id: game-repo
            description: Atom games monorepo
            writable: true
            lockable: true
            is_git_repo: true
            repo_url: git@github.com:atom/games.git
            default_lock_mode: branch_isolated
            auto_attach: false
            ---

            # game-repo

            Body text used as description fallback.
            """
        ).strip()
    )
    kind = parse_workspace_kind_file(md, project_id="p1")
    assert kind.id == "game-repo"
    assert kind.project_id == "p1"
    assert kind.description == "Atom games monorepo"
    assert kind.writable
    assert kind.lockable
    assert kind.is_git_repo
    assert kind.repo_url == "git@github.com:atom/games.git"
    assert kind.default_lock_mode == "branch_isolated"
    assert kind.auto_attach is False


def test_parse_uses_body_when_description_missing(tmp_path: Path):
    md = tmp_path / "k.md"
    md.write_text(
        textwrap.dedent(
            """
            ---
            id: k
            ---

            # k

            Body text.

            Second paragraph.
            """
        ).strip()
    )
    kind = parse_workspace_kind_file(md, project_id="__system__")
    assert kind.description == "Body text.\n\nSecond paragraph."


def test_parse_defaults(tmp_path: Path):
    md = tmp_path / "minimal.md"
    md.write_text("---\nid: minimal\n---\n")
    kind = parse_workspace_kind_file(md, project_id="__system__")
    assert kind.writable is True
    assert kind.lockable is True
    assert kind.is_git_repo is True
    assert kind.auto_attach is False
    assert kind.repo_url is None
    assert kind.default_lock_mode is None


def test_parse_rejects_missing_id(tmp_path: Path):
    md = tmp_path / "noid.md"
    md.write_text("---\nwritable: true\n---\n")
    with pytest.raises(ValueError, match="missing.*id"):
        parse_workspace_kind_file(md, project_id="__system__")


def test_parse_rejects_invalid_lock_mode(tmp_path: Path):
    md = tmp_path / "bad-lock.md"
    md.write_text("---\nid: bad\ndefault_lock_mode: WHATEVER\n---\n")
    with pytest.raises(ValueError, match="default_lock_mode"):
        parse_workspace_kind_file(md, project_id="__system__")


def test_parse_no_frontmatter_at_all(tmp_path: Path):
    md = tmp_path / "plain.md"
    md.write_text("# Just markdown body, no frontmatter\n")
    with pytest.raises(ValueError, match="missing.*id"):
        parse_workspace_kind_file(md, project_id="__system__")


def test_parse_explicit_description_wins_over_body(tmp_path: Path):
    md = tmp_path / "winner.md"
    md.write_text(
        textwrap.dedent(
            """
            ---
            id: winner
            description: Frontmatter wins
            ---

            # winner

            Body text that should be ignored.
            """
        ).strip()
    )
    kind = parse_workspace_kind_file(md, project_id="__system__")
    assert kind.description == "Frontmatter wins"
