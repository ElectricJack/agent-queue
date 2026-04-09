"""Tests for vault directory structure initialization (vault spec §2)
and Obsidian config migration (vault spec §6, Phase 1).
"""

from __future__ import annotations

from src.vault import (
    ensure_vault_layout,
    ensure_vault_profile_dirs,
    ensure_vault_project_dirs,
    migrate_obsidian_config,
)


def test_ensure_vault_layout_creates_static_dirs(tmp_path):
    """Static vault directories are created under data_dir."""
    ensure_vault_layout(str(tmp_path))

    expected = [
        "vault/.obsidian",
        "vault/system/playbooks",
        "vault/system/memory",
        "vault/orchestrator/playbooks",
        "vault/orchestrator/memory",
        "vault/agent-types",
        "vault/projects",
        "vault/templates",
    ]
    for subdir in expected:
        assert (tmp_path / subdir).is_dir(), f"Missing directory: {subdir}"


def test_ensure_vault_layout_idempotent(tmp_path):
    """Calling ensure_vault_layout twice does not raise."""
    ensure_vault_layout(str(tmp_path))
    ensure_vault_layout(str(tmp_path))

    assert (tmp_path / "vault" / "system" / "playbooks").is_dir()


def test_ensure_vault_profile_dirs(tmp_path):
    """Per-profile vault directories are created correctly."""
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    base = tmp_path / "vault" / "agent-types" / "coding"
    assert (base / "playbooks").is_dir()
    assert (base / "memory").is_dir()


def test_ensure_vault_profile_dirs_idempotent(tmp_path):
    """Calling ensure_vault_profile_dirs twice does not raise."""
    ensure_vault_profile_dirs(str(tmp_path), "coding")
    ensure_vault_profile_dirs(str(tmp_path), "coding")

    assert (tmp_path / "vault" / "agent-types" / "coding" / "playbooks").is_dir()


def test_ensure_vault_project_dirs(tmp_path):
    """Per-project vault directories are created with full subtree."""
    ensure_vault_project_dirs(str(tmp_path), "mech-fighters")

    base = tmp_path / "vault" / "projects" / "mech-fighters"
    expected_subdirs = [
        "memory/knowledge",
        "memory/insights",
        "playbooks",
        "notes",
        "references",
        "overrides",
    ]
    for subdir in expected_subdirs:
        assert (base / subdir).is_dir(), f"Missing project subdir: {subdir}"


def test_ensure_vault_project_dirs_idempotent(tmp_path):
    """Calling ensure_vault_project_dirs twice does not raise."""
    ensure_vault_project_dirs(str(tmp_path), "my-project")
    ensure_vault_project_dirs(str(tmp_path), "my-project")

    assert (tmp_path / "vault" / "projects" / "my-project" / "playbooks").is_dir()


# ---------------------------------------------------------------------------
# migrate_obsidian_config (vault spec §6, Phase 1)
# ---------------------------------------------------------------------------


def test_migrate_obsidian_config_moves_directory(tmp_path):
    """When source exists and destination does not, .obsidian is moved."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    # Populate with representative Obsidian config files
    (source / "app.json").write_text('{"theme": "dark"}')
    (source / "workspace.json").write_text('{"active": "notes"}')
    plugins_dir = source / "plugins" / "dataview"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "main.js").write_text("// plugin code")

    result = migrate_obsidian_config(str(tmp_path))

    assert result is True
    dest = tmp_path / "vault" / ".obsidian"
    assert dest.is_dir()
    assert (dest / "app.json").read_text() == '{"theme": "dark"}'
    assert (dest / "workspace.json").read_text() == '{"active": "notes"}'
    assert (dest / "plugins" / "dataview" / "main.js").read_text() == "// plugin code"
    # Source should no longer exist
    assert not source.exists()


def test_migrate_obsidian_config_skips_when_source_missing(tmp_path):
    """When source does not exist, nothing happens."""
    result = migrate_obsidian_config(str(tmp_path))

    assert result is False
    # Destination should not have been created either
    assert not (tmp_path / "vault" / ".obsidian").exists()


def test_migrate_obsidian_config_skips_when_dest_exists(tmp_path):
    """When destination already exists, source is left untouched."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    (source / "app.json").write_text('{"theme": "dark"}')

    dest = tmp_path / "vault" / ".obsidian"
    dest.mkdir(parents=True)
    (dest / "existing.json").write_text('{"keep": true}')

    result = migrate_obsidian_config(str(tmp_path))

    assert result is False
    # Source still exists (not moved)
    assert source.is_dir()
    assert (source / "app.json").read_text() == '{"theme": "dark"}'
    # Destination retains its original content
    assert (dest / "existing.json").read_text() == '{"keep": true}'


def test_migrate_obsidian_config_idempotent(tmp_path):
    """Calling migrate twice — the second call is a no-op."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    (source / "app.json").write_text('{"theme": "dark"}')

    assert migrate_obsidian_config(str(tmp_path)) is True
    # Source gone after first call; second call should skip gracefully
    assert migrate_obsidian_config(str(tmp_path)) is False

    dest = tmp_path / "vault" / ".obsidian"
    assert dest.is_dir()
    assert (dest / "app.json").read_text() == '{"theme": "dark"}'


def test_migrate_obsidian_config_creates_vault_parent(tmp_path):
    """Vault parent directory is created if it doesn't exist yet."""
    source = tmp_path / "memory" / ".obsidian"
    source.mkdir(parents=True)
    (source / "themes.json").write_text("{}")

    # No vault/ directory exists yet
    assert not (tmp_path / "vault").exists()

    result = migrate_obsidian_config(str(tmp_path))

    assert result is True
    assert (tmp_path / "vault" / ".obsidian" / "themes.json").read_text() == "{}"


def test_multiple_profiles_and_projects(tmp_path):
    """Multiple profiles and projects can coexist under vault."""
    ensure_vault_layout(str(tmp_path))

    for profile_id in ("coding", "code-review", "qa"):
        ensure_vault_profile_dirs(str(tmp_path), profile_id)

    for project_id in ("project-a", "project-b"):
        ensure_vault_project_dirs(str(tmp_path), project_id)

    # Verify all exist
    agent_types = tmp_path / "vault" / "agent-types"
    assert (agent_types / "coding" / "playbooks").is_dir()
    assert (agent_types / "code-review" / "memory").is_dir()
    assert (agent_types / "qa" / "playbooks").is_dir()

    projects = tmp_path / "vault" / "projects"
    assert (projects / "project-a" / "overrides").is_dir()
    assert (projects / "project-b" / "references").is_dir()
