"""Tests for the default agent-type playbook installer.

Verifies ``ensure_default_agent_type_playbooks`` copies bundled agent-type
playbooks from ``src/prompts/default_agent_type_playbooks/`` into
``{data_dir}/vault/agent-types/{type}/playbooks/`` on startup, and is
idempotent (skips existing files).
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

EXPECTED_CLAUDE_CODE_FILES = {
    "reflection.md",
}


def test_source_tree_has_expected_playbooks() -> None:
    """Sanity check: the bundled source files exist at the expected path."""
    claude_code_dir = SRC_ROOT / "claude-code"
    assert claude_code_dir.is_dir()

    claude_code_files = {
        p.name for p in claude_code_dir.iterdir() if p.suffix == ".md"
    }
    assert claude_code_files == EXPECTED_CLAUDE_CODE_FILES

    # No legacy subdirs — supervisor and coding scopes were retired.
    assert not (SRC_ROOT / "supervisor").exists()
    assert not (SRC_ROOT / "coding").exists()


def test_clean_install_creates_all_playbooks(tmp_path):
    result = ensure_default_agent_type_playbooks(str(tmp_path))

    claude_code_dir = tmp_path / "vault" / "agent-types" / "claude-code" / "playbooks"

    for name in EXPECTED_CLAUDE_CODE_FILES:
        assert (claude_code_dir / name).is_file()

    expected_created = {f"claude-code/{name}" for name in EXPECTED_CLAUDE_CODE_FILES}
    assert set(result["created"]) == expected_created
    assert result["skipped"] == []


def test_idempotent_on_second_install(tmp_path):
    first = ensure_default_agent_type_playbooks(str(tmp_path))
    second = ensure_default_agent_type_playbooks(str(tmp_path))

    # Second run should have skipped everything the first run created.
    assert second["created"] == []
    assert set(second["skipped"]) == set(first["created"])


def test_user_customisations_preserved(tmp_path):
    """Existing files in the vault are never overwritten by the installer."""
    target_dir = tmp_path / "vault" / "agent-types" / "claude-code" / "playbooks"
    target_dir.mkdir(parents=True)
    customised = target_dir / "reflection.md"
    customised.write_text("user-customised content\n", encoding="utf-8")

    ensure_default_agent_type_playbooks(str(tmp_path))

    # The user's content must survive.
    assert customised.read_text(encoding="utf-8") == "user-customised content\n"


def test_no_source_dir_returns_empty(tmp_path, monkeypatch):
    """If the source directory is missing, the installer is a no-op."""
    # Simulate a package with no bundled default_agent_type_playbooks dir by
    # pointing __file__ at an empty tmp location.
    import src.vault as vault_module

    fake_src = tmp_path / "fake_src"
    fake_src.mkdir()
    fake_vault = tmp_path / "fake_vault"
    monkeypatch.setattr(vault_module, "__file__", str(fake_src / "vault.py"))

    result = ensure_default_agent_type_playbooks(str(fake_vault))
    assert result == {"created": [], "skipped": []}
