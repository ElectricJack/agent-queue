"""Tests for vault directory structure initialization (vault spec §2)."""

from __future__ import annotations

from src.vault import (
    ensure_vault_layout,
    ensure_vault_profile_dirs,
    ensure_vault_project_dirs,
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
