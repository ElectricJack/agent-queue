"""Tests for the default agent-type playbook installer.

``ensure_default_agent_type_playbooks`` copies bundled agent-type playbooks
from ``src/prompts/default_agent_type_playbooks/`` into
``{data_dir}/vault/agent-types/{type}/playbooks/`` on startup.  The tree ships
none since the Playbook V2 cutover retired ``claude-opus/reflection.md``
without a replacement, so the installer must be a clean no-op that still
never touches an operator's own files.
"""

from __future__ import annotations

from pathlib import Path

from src.vault import ensure_default_agent_type_playbooks


SRC_ROOT = (
    Path(__file__).parent.parent
    / "src"
    / "prompts"
    / "default_agent_type_playbooks"
)


def test_source_tree_ships_no_agent_type_playbooks() -> None:
    """The retired scopes stay retired: nothing under the bundled root."""
    bundled = sorted(SRC_ROOT.rglob("*.md")) if SRC_ROOT.exists() else []
    assert bundled == []
    for legacy in ("supervisor", "coding", "claude-code", "claude-opus"):
        assert not (SRC_ROOT / legacy).exists()


def test_clean_install_creates_nothing(tmp_path):
    result = ensure_default_agent_type_playbooks(str(tmp_path))
    assert result == {"created": [], "skipped": []}
    assert not (tmp_path / "vault" / "agent-types").exists()


def test_user_customisations_preserved(tmp_path):
    """Existing files in the vault are never overwritten by the installer."""
    target_dir = tmp_path / "vault" / "agent-types" / "claude-opus" / "playbooks"
    target_dir.mkdir(parents=True)
    customised = target_dir / "reflection.md"
    customised.write_text("user-customised content\n", encoding="utf-8")

    ensure_default_agent_type_playbooks(str(tmp_path))

    assert customised.read_text(encoding="utf-8") == "user-customised content\n"


def test_no_source_dir_returns_empty(tmp_path, monkeypatch):
    """If the source directory is missing, the installer is a no-op."""
    import src.vault as vault_module

    fake_src = tmp_path / "fake_src"
    fake_src.mkdir()
    fake_vault = tmp_path / "fake_vault"
    monkeypatch.setattr(vault_module, "__file__", str(fake_src / "vault.py"))

    result = ensure_default_agent_type_playbooks(str(fake_vault))
    assert result == {"created": [], "skipped": []}
