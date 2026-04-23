"""Tests for ``aq://`` URI rewriting (compile-time)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from src.aq_uri import AqUriError, is_aq_uri, rewrite_aq_uris


@dataclass
class FakeConfig:
    data_dir: str
    vault_root: str


@pytest.fixture
def config(tmp_path: Path) -> FakeConfig:
    return FakeConfig(data_dir=str(tmp_path), vault_root=str(tmp_path / "vault"))


# ---------------------------------------------------------------------------
# is_aq_uri
# ---------------------------------------------------------------------------


def test_is_aq_uri_recognises_scheme():
    assert is_aq_uri("aq://prompts/foo.md")
    assert is_aq_uri("aq://vault/projects/x/memory.md")


def test_is_aq_uri_rejects_other_schemes():
    assert not is_aq_uri("/abs/path")
    assert not is_aq_uri("file:///tmp/x")
    assert not is_aq_uri("prompts/foo.md")
    assert not is_aq_uri(None)


# ---------------------------------------------------------------------------
# rewrite_aq_uris — singleton authorities
# ---------------------------------------------------------------------------


def test_rewrite_vault_uri(config: FakeConfig):
    text = 'read_file(path="aq://vault/projects/p1/memory/x.md")'
    out = rewrite_aq_uris(text, config=config)
    assert out == f'read_file(path="{config.vault_root}/projects/p1/memory/x.md")'


def test_rewrite_prompts_uri_uses_bundled_dir(config: FakeConfig):
    out = rewrite_aq_uris('"aq://prompts/consolidation_task.md"', config=config)
    expected_root = str(Path(__file__).parent.parent / "src" / "prompts")
    assert out == f'"{expected_root}/consolidation_task.md"'


def test_rewrite_logs_uri(config: FakeConfig):
    out = rewrite_aq_uris("aq://logs/app.log", config=config)
    assert out == f"{config.data_dir}/logs/app.log"


def test_rewrite_tasks_uri(config: FakeConfig):
    out = rewrite_aq_uris("aq://tasks/t-1/out.txt", config=config)
    assert out == f"{config.data_dir}/tasks/t-1/out.txt"


def test_rewrite_attachments_uri(config: FakeConfig):
    out = rewrite_aq_uris("aq://attachments/abc.png", config=config)
    assert out == f"{config.data_dir}/attachments/abc.png"


# ---------------------------------------------------------------------------
# rewrite_aq_uris — multiple URIs, context preservation
# ---------------------------------------------------------------------------


def test_rewrite_preserves_runtime_placeholders(config: FakeConfig):
    """<project_id> is a runtime placeholder the LLM fills at step time;
    the rewrite must leave it intact."""
    text = 'path="aq://vault/projects/<project_id>/memory/consolidation.md"'
    out = rewrite_aq_uris(text, config=config)
    assert "<project_id>" in out
    assert out.startswith(f'path="{config.vault_root}/projects/<project_id>')


def test_rewrite_handles_multiple_uris(config: FakeConfig):
    text = (
        'read_file(path="aq://vault/a.md")\n'
        'render_prompt(path="aq://prompts/b.md")'
    )
    out = rewrite_aq_uris(text, config=config)
    assert "aq://" not in out
    assert f'{config.vault_root}/a.md' in out


def test_rewrite_leaves_non_uri_text_alone(config: FakeConfig):
    text = "normal text without URIs\nand some `aq://` mentions with no path"
    out = rewrite_aq_uris(text, config=config)
    assert out == text


# ---------------------------------------------------------------------------
# rewrite_aq_uris — error cases
# ---------------------------------------------------------------------------


def test_rewrite_rejects_unknown_authority(config: FakeConfig):
    with pytest.raises(AqUriError, match="Unknown aq:// authority"):
        rewrite_aq_uris("aq://nope/x", config=config)


def test_rewrite_rejects_workspace_authority(config: FakeConfig):
    """Workspace authorities are intentionally not supported at compile time."""
    with pytest.raises(AqUriError):
        rewrite_aq_uris("aq://workspace/p1/x", config=config)


def test_rewrite_rejects_traversal(config: FakeConfig):
    with pytest.raises(AqUriError, match="rejects '..'"):
        rewrite_aq_uris("aq://vault/../etc/passwd", config=config)
