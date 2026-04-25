"""Tests for path= validation in _cmd_read_prompt / _cmd_render_prompt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@dataclass
class FakeConfig:
    data_dir: str
    vault_root: str


class FakeHandler:
    """Minimal stand-in that includes just _load_prompt_from_path.

    We import the mixin class and instantiate a thin subclass with the
    attributes the helper reads (self.config)."""

    def __init__(self, config):
        self.config = config


@pytest.fixture
def config(tmp_path: Path) -> FakeConfig:
    vault = tmp_path / "vault"
    vault.mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "tasks").mkdir()
    (tmp_path / "attachments").mkdir()
    return FakeConfig(data_dir=str(tmp_path), vault_root=str(vault))


def _make_handler(config):
    # Pull the mixin and bind just what the helper needs.
    from src.commands.system_commands import SystemCommandsMixin

    h = FakeHandler(config)
    h._load_prompt_from_path = SystemCommandsMixin._load_prompt_from_path.__get__(h)
    return h


def test_load_prompt_rejects_path_outside_allowed_roots(config, tmp_path):
    outside = tmp_path.parent / "outside.md"
    outside.write_text("---\nname: foo\n---\nbody\n")

    handler = _make_handler(config)
    tmpl, err = handler._load_prompt_from_path(str(outside))
    assert tmpl is None
    assert err is not None
    assert "outside" in err["error"].lower() or "allowed" in err["error"].lower()


def test_load_prompt_accepts_path_under_vault_root(config):
    p = Path(config.vault_root) / "a.md"
    p.write_text("---\nname: a\n---\nhello\n")

    handler = _make_handler(config)
    tmpl, err = handler._load_prompt_from_path(str(p))
    assert err is None
    assert tmpl is not None
    assert tmpl.name == "a"


def test_load_prompt_rejects_relative_path(config):
    handler = _make_handler(config)
    tmpl, err = handler._load_prompt_from_path("relative.md")
    assert tmpl is None
    assert "absolute" in err["error"].lower()
