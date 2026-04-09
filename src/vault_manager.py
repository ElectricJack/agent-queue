"""Central vault path resolution and directory management.

``VaultManager`` is the single entry-point other subsystems use to obtain
vault paths and to ensure the on-disk directory structure exists.  It wraps
the vault path properties on :class:`~src.config.AppConfig` and the
directory-creation helpers in :mod:`src.vault`, providing a unified,
object-oriented API.

Usage::

    vm = VaultManager(app_config)

    # Resolve paths without side-effects
    project_dir = vm.get_project_dir("mech-fighters")
    playbook     = vm.get_playbook_path("project", "mech-fighters")

    # Ensure directories exist (idempotent)
    vm.ensure_layout()
    vm.register_project("mech-fighters")
    vm.register_agent_type("coding")

See ``docs/specs/design/vault.md`` Section 2 for the full directory layout.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal

from src.vault import (
    ensure_vault_layout,
    ensure_vault_profile_dirs,
    ensure_vault_project_dirs,
)

if TYPE_CHECKING:
    from src.config import AppConfig

logger = logging.getLogger(__name__)

# Supported scopes for path helpers.  "system" and "orchestrator" are singletons;
# "agent_type" and "project" require an additional identifier.
Scope = Literal["system", "orchestrator", "agent_type", "project"]


class VaultManager:
    """Resolve vault paths and manage the on-disk directory structure.

    All path resolution methods are pure — they return paths without touching
    the filesystem.  Directory creation is always explicit via the ``ensure_*``
    / ``register_*`` family of methods and is idempotent (safe to call
    repeatedly).

    Parameters
    ----------
    config:
        The application configuration.  Only ``data_dir`` and the derived
        ``vault_*`` / ``compiled_root`` properties are read.
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    # ------------------------------------------------------------------
    # Top-level directory properties (delegated to AppConfig properties)
    # ------------------------------------------------------------------

    @property
    def vault_root(self) -> str:
        """Root of the Obsidian-compatible vault."""
        return self._config.vault_root

    @property
    def system_dir(self) -> str:
        """System-scoped vault directory."""
        return self._config.vault_system

    @property
    def orchestrator_dir(self) -> str:
        """Orchestrator vault directory."""
        return self._config.vault_orchestrator

    @property
    def agent_types_dir(self) -> str:
        """Root directory for all agent-type profiles."""
        return self._config.vault_agent_types

    @property
    def projects_dir(self) -> str:
        """Root directory for all per-project vaults."""
        return self._config.vault_projects

    @property
    def templates_dir(self) -> str:
        """Templates directory."""
        return self._config.vault_templates

    @property
    def compiled_root(self) -> str:
        """Compiled playbook JSON (runtime artifacts)."""
        return self._config.compiled_root

    # ------------------------------------------------------------------
    # Path resolution — per-entity directories
    # ------------------------------------------------------------------

    def get_project_dir(self, project_id: str) -> str:
        """Return the vault directory for *project_id*.

        Does **not** create the directory — call :meth:`register_project`
        for that.
        """
        return os.path.join(self.projects_dir, project_id)

    def get_agent_type_dir(self, agent_type: str) -> str:
        """Return the vault directory for an agent-type profile.

        Does **not** create the directory — call :meth:`register_agent_type`
        for that.
        """
        return os.path.join(self.agent_types_dir, agent_type)

    # ------------------------------------------------------------------
    # Path resolution — scoped helpers
    # ------------------------------------------------------------------

    def get_playbook_path(self, scope: Scope, identifier: str | None = None) -> str:
        """Return the playbooks directory for the given *scope*.

        Parameters
        ----------
        scope:
            One of ``"system"``, ``"orchestrator"``, ``"agent_type"``, or
            ``"project"``.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes — the
            profile id or project id respectively.  Ignored for singleton
            scopes.

        Returns
        -------
        str
            Absolute path to the playbooks directory.

        Raises
        ------
        ValueError
            If *identifier* is required but not supplied.
        """
        return os.path.join(self._scope_dir(scope, identifier), "playbooks")

    def get_memory_path(self, scope: Scope, identifier: str | None = None) -> str:
        """Return the memory directory for the given *scope*.

        Parameters are the same as :meth:`get_playbook_path`.
        """
        return os.path.join(self._scope_dir(scope, identifier), "memory")

    def get_profile_path(self, agent_type: str) -> str:
        """Return the path to the profile definition file for *agent_type*.

        By convention this is ``vault/agent-types/{agent_type}/profile.md``.
        """
        return os.path.join(self.get_agent_type_dir(agent_type), "profile.md")

    def get_facts_path(self, scope: Scope, identifier: str | None = None) -> str:
        """Return the path to the facts file for the given *scope*.

        Facts files are KV source-of-truth files stored inside the memory
        directory of each scope: ``{scope_dir}/memory/facts.md``.
        """
        return os.path.join(self.get_memory_path(scope, identifier), "facts.md")

    def get_overrides_dir(self, project_id: str) -> str:
        """Return the overrides directory for *project_id*."""
        return os.path.join(self.get_project_dir(project_id), "overrides")

    def get_notes_dir(self, project_id: str) -> str:
        """Return the notes directory for *project_id*."""
        return os.path.join(self.get_project_dir(project_id), "notes")

    def get_references_dir(self, project_id: str) -> str:
        """Return the references directory for *project_id*."""
        return os.path.join(self.get_project_dir(project_id), "references")

    def get_knowledge_dir(self, project_id: str) -> str:
        """Return the knowledge directory for *project_id*.

        This is ``vault/projects/{project_id}/memory/knowledge/``.
        """
        return os.path.join(self.get_project_dir(project_id), "memory", "knowledge")

    def get_insights_dir(self, project_id: str) -> str:
        """Return the insights directory for *project_id*.

        This is ``vault/projects/{project_id}/memory/insights/``.
        """
        return os.path.join(self.get_project_dir(project_id), "memory", "insights")

    # ------------------------------------------------------------------
    # Directory creation (idempotent)
    # ------------------------------------------------------------------

    def ensure_layout(self) -> None:
        """Create the static vault directory tree.

        This is idempotent — calling it when the directories already exist
        is a safe no-op.  Delegates to :func:`src.vault.ensure_vault_layout`.
        """
        ensure_vault_layout(self._config.data_dir)

    def register_project(self, project_id: str) -> str:
        """Ensure vault subdirectories exist for *project_id*.

        Creates ``memory/knowledge``, ``memory/insights``, ``playbooks``,
        ``notes``, ``references``, and ``overrides`` under the project
        directory.  Idempotent.

        Returns the absolute path to the project directory.
        """
        ensure_vault_project_dirs(self._config.data_dir, project_id)
        project_dir = self.get_project_dir(project_id)
        logger.debug("Registered vault project: %s → %s", project_id, project_dir)
        return project_dir

    def register_agent_type(self, agent_type: str) -> str:
        """Ensure vault subdirectories exist for *agent_type*.

        Creates ``playbooks`` and ``memory`` under the agent-type directory.
        Idempotent.

        Returns the absolute path to the agent-type directory.
        """
        ensure_vault_profile_dirs(self._config.data_dir, agent_type)
        agent_dir = self.get_agent_type_dir(agent_type)
        logger.debug("Registered vault agent-type: %s → %s", agent_type, agent_dir)
        return agent_dir

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _scope_dir(self, scope: Scope, identifier: str | None) -> str:
        """Resolve the base directory for a scope + optional identifier."""
        if scope == "system":
            return self.system_dir
        if scope == "orchestrator":
            return self.orchestrator_dir
        if scope == "agent_type":
            if not identifier:
                raise ValueError("'agent_type' scope requires an identifier")
            return self.get_agent_type_dir(identifier)
        if scope == "project":
            if not identifier:
                raise ValueError("'project' scope requires an identifier")
            return self.get_project_dir(identifier)
        raise ValueError(f"Unknown scope: {scope!r}")
