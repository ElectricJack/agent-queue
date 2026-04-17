"""Tests for VaultManager (vault spec §2 — central path resolution)."""

from __future__ import annotations

import os

import pytest

from src.config import AppConfig
from src.vault_manager import VaultManager


@pytest.fixture()
def vm(tmp_path) -> VaultManager:
    """VaultManager wired to a temporary data directory."""
    config = AppConfig(data_dir=str(tmp_path))
    return VaultManager(config)


# ---- Top-level properties ------------------------------------------------


class TestProperties:
    def test_vault_root(self, vm: VaultManager, tmp_path):
        assert vm.vault_root == os.path.join(str(tmp_path), "vault")

    def test_system_dir(self, vm: VaultManager, tmp_path):
        assert vm.system_dir == os.path.join(str(tmp_path), "vault", "system")

    def test_orchestrator_dir(self, vm: VaultManager, tmp_path):
        assert vm.orchestrator_dir == os.path.join(str(tmp_path), "vault", "orchestrator")

    def test_agent_types_dir(self, vm: VaultManager, tmp_path):
        assert vm.agent_types_dir == os.path.join(str(tmp_path), "vault", "agent-types")

    def test_projects_dir(self, vm: VaultManager, tmp_path):
        assert vm.projects_dir == os.path.join(str(tmp_path), "vault", "projects")

    def test_templates_dir(self, vm: VaultManager, tmp_path):
        assert vm.templates_dir == os.path.join(str(tmp_path), "vault", "templates")

    def test_compiled_root(self, vm: VaultManager, tmp_path):
        assert vm.compiled_root == os.path.join(str(tmp_path), "compiled")


# ---- Per-entity directory resolution -------------------------------------


class TestEntityDirs:
    def test_get_project_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_project_dir("mech-fighters")
        assert result == os.path.join(str(tmp_path), "vault", "projects", "mech-fighters")

    def test_get_agent_type_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_agent_type_dir("coding")
        assert result == os.path.join(str(tmp_path), "vault", "agent-types", "coding")


# ---- Scoped helpers: playbooks, memory, profiles, facts ------------------


class TestScopedHelpers:
    def test_get_playbook_path_system(self, vm: VaultManager, tmp_path):
        result = vm.get_playbook_path("system")
        assert result == os.path.join(str(tmp_path), "vault", "system", "playbooks")

    def test_get_playbook_path_orchestrator(self, vm: VaultManager, tmp_path):
        result = vm.get_playbook_path("orchestrator")
        assert result == os.path.join(str(tmp_path), "vault", "orchestrator", "playbooks")

    def test_get_playbook_path_agent_type(self, vm: VaultManager, tmp_path):
        result = vm.get_playbook_path("agent_type", "coding")
        assert result == os.path.join(str(tmp_path), "vault", "agent-types", "coding", "playbooks")

    def test_get_playbook_path_project(self, vm: VaultManager, tmp_path):
        result = vm.get_playbook_path("project", "my-app")
        assert result == os.path.join(str(tmp_path), "vault", "projects", "my-app", "playbooks")

    def test_get_playbook_path_agent_type_requires_id(self, vm: VaultManager):
        with pytest.raises(ValueError, match="requires an identifier"):
            vm.get_playbook_path("agent_type")

    def test_get_playbook_path_project_requires_id(self, vm: VaultManager):
        with pytest.raises(ValueError, match="requires an identifier"):
            vm.get_playbook_path("project")

    def test_get_memory_path_system(self, vm: VaultManager, tmp_path):
        result = vm.get_memory_path("system")
        assert result == os.path.join(str(tmp_path), "vault", "system", "memory")

    def test_get_memory_path_project(self, vm: VaultManager, tmp_path):
        result = vm.get_memory_path("project", "my-app")
        assert result == os.path.join(str(tmp_path), "vault", "projects", "my-app", "memory")

    def test_get_profile_path(self, vm: VaultManager, tmp_path):
        result = vm.get_profile_path("coding")
        assert result == os.path.join(str(tmp_path), "vault", "agent-types", "coding", "profile.md")

    def test_get_facts_path_system(self, vm: VaultManager, tmp_path):
        result = vm.get_facts_path("system")
        assert result == os.path.join(str(tmp_path), "vault", "system", "memory", "facts.md")

    def test_get_facts_path_orchestrator(self, vm: VaultManager, tmp_path):
        result = vm.get_facts_path("orchestrator")
        assert result == os.path.join(str(tmp_path), "vault", "orchestrator", "memory", "facts.md")

    def test_get_facts_path_agent_type(self, vm: VaultManager, tmp_path):
        result = vm.get_facts_path("agent_type", "coding")
        assert result == os.path.join(
            str(tmp_path), "vault", "agent-types", "coding", "memory", "facts.md"
        )

    def test_get_facts_path_project(self, vm: VaultManager, tmp_path):
        result = vm.get_facts_path("project", "my-app")
        assert result == os.path.join(
            str(tmp_path), "vault", "projects", "my-app", "memory", "facts.md"
        )

    def test_unknown_scope_raises(self, vm: VaultManager):
        with pytest.raises(ValueError, match="Unknown scope"):
            vm.get_playbook_path("unknown")  # type: ignore[arg-type]


# ---- Project-specific path helpers ---------------------------------------


class TestProjectHelpers:
    def test_get_overrides_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_overrides_dir("my-app")
        assert result == os.path.join(str(tmp_path), "vault", "projects", "my-app", "overrides")

    def test_get_notes_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_notes_dir("my-app")
        assert result == os.path.join(str(tmp_path), "vault", "projects", "my-app", "notes")

    def test_get_references_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_references_dir("my-app")
        assert result == os.path.join(str(tmp_path), "vault", "projects", "my-app", "references")

    def test_get_knowledge_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_knowledge_dir("my-app")
        assert result == os.path.join(
            str(tmp_path), "vault", "projects", "my-app", "memory", "knowledge"
        )

    def test_get_insights_dir(self, vm: VaultManager, tmp_path):
        result = vm.get_insights_dir("my-app")
        assert result == os.path.join(
            str(tmp_path), "vault", "projects", "my-app", "memory", "insights"
        )


# ---- Directory creation --------------------------------------------------


class TestDirectoryCreation:
    def test_ensure_layout_creates_static_dirs(self, vm: VaultManager, tmp_path):
        vm.ensure_layout()

        for subdir in (
            "vault/.obsidian",
            "vault/system/playbooks",
            "vault/system/memory",
            "vault/orchestrator/playbooks",
            "vault/orchestrator/memory",
            "vault/agent-types",
            "vault/projects",
            "vault/templates",
        ):
            assert (tmp_path / subdir).is_dir(), f"Missing: {subdir}"

    def test_ensure_layout_idempotent(self, vm: VaultManager, tmp_path):
        vm.ensure_layout()
        vm.ensure_layout()
        assert (tmp_path / "vault" / "system" / "playbooks").is_dir()

    def test_register_project_creates_subdirs(self, vm: VaultManager, tmp_path):
        result = vm.register_project("my-app")

        assert result == str(tmp_path / "vault" / "projects" / "my-app")
        base = tmp_path / "vault" / "projects" / "my-app"
        for subdir in (
            "memory/knowledge",
            "memory/insights",
            "playbooks",
            "notes",
            "references",
            "overrides",
        ):
            assert (base / subdir).is_dir(), f"Missing project subdir: {subdir}"

    def test_register_project_idempotent(self, vm: VaultManager, tmp_path):
        vm.register_project("my-app")
        vm.register_project("my-app")
        assert (tmp_path / "vault" / "projects" / "my-app" / "playbooks").is_dir()

    def test_register_agent_type_creates_subdirs(self, vm: VaultManager, tmp_path):
        result = vm.register_agent_type("coding")

        assert result == str(tmp_path / "vault" / "agent-types" / "coding")
        base = tmp_path / "vault" / "agent-types" / "coding"
        assert (base / "playbooks").is_dir()
        assert (base / "memory").is_dir()

    def test_register_agent_type_idempotent(self, vm: VaultManager, tmp_path):
        vm.register_agent_type("coding")
        vm.register_agent_type("coding")
        assert (tmp_path / "vault" / "agent-types" / "coding" / "memory").is_dir()

    def test_register_multiple_projects_and_agent_types(self, vm: VaultManager, tmp_path):
        vm.ensure_layout()

        for at in ("coding", "code-review", "qa"):
            vm.register_agent_type(at)
        for proj in ("alpha", "beta"):
            vm.register_project(proj)

        assert (tmp_path / "vault" / "agent-types" / "code-review" / "playbooks").is_dir()
        assert (tmp_path / "vault" / "projects" / "beta" / "overrides").is_dir()


# ---- Tracks data_dir changes dynamically ---------------------------------


class TestDynamic:
    def test_paths_track_data_dir(self, tmp_path):
        """If data_dir changes on the config, VaultManager reflects it."""
        config = AppConfig(data_dir=str(tmp_path / "first"))
        vm = VaultManager(config)
        assert "first" in vm.vault_root

        config.data_dir = str(tmp_path / "second")
        assert "second" in vm.vault_root
        assert "second" in vm.get_project_dir("x")
