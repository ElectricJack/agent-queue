"""Compiled playbook JSON storage with scope-mirrored directory structure.

Stores compiled playbook JSON files in ``~/.agent-queue/compiled/`` using a
directory layout that mirrors the vault's scope structure.  This keeps compiled
(generated) artifacts separate from hand-authored markdown while maintaining a
predictable, scope-aware path scheme.

Directory structure::

    ~/.agent-queue/compiled/
      system/
        task-outcome.compiled.json
        system-health.compiled.json
      orchestrator/
        task-assignment.compiled.json
      agent-types/
        coding/
          coding-standards.compiled.json
      projects/
        mech-fighters/
          code-quality-gate.compiled.json

See ``docs/specs/design/playbooks.md`` Section 8 — Scoping / Storage.

Typical usage::

    from src.playbook_store import CompiledPlaybookStore

    store = CompiledPlaybookStore(vault_manager)

    # Save after compilation
    path = store.save(compiled_playbook, scope="system")

    # Load at runtime
    playbook = store.load("task-outcome", scope="system")

    # Check if recompilation is needed
    if store.needs_recompile("task-outcome", new_source_hash, scope="system"):
        result = await compiler.compile(markdown)
        store.save(result.playbook, scope="system")
"""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from src.playbook_models import CompiledPlaybook

if TYPE_CHECKING:
    from src.vault_manager import Scope, VaultManager

logger = logging.getLogger(__name__)

# File suffix for compiled playbook JSON files.
COMPILED_SUFFIX = ".compiled.json"


class CompiledPlaybookStore:
    """Read/write compiled playbook JSON to the scope-mirrored ``compiled/`` tree.

    All file I/O is synchronous — compiled JSON files are small (typically
    under 10 KB) and writes are infrequent (only on recompilation).  The
    store creates directories on-demand when saving; no up-front layout
    initialization is needed.

    Parameters
    ----------
    vault_manager:
        Provides ``compiled_root`` and scope resolution.  Only the
        ``compiled_root`` property is used; vault (source) paths are not
        touched.
    """

    def __init__(self, vault_manager: VaultManager) -> None:
        self._compiled_root = vault_manager.compiled_root

    # ------------------------------------------------------------------
    # Path resolution
    # ------------------------------------------------------------------

    def _scope_dir(self, scope: Scope, identifier: str | None) -> str:
        """Return the compiled directory for a given scope.

        Maps scope names to the same directory hierarchy used in the vault::

            system       → compiled/system/
            orchestrator → compiled/orchestrator/
            agent_type   → compiled/agent-types/{identifier}/
            project      → compiled/projects/{identifier}/

        Raises
        ------
        ValueError
            If an identifier is required but not supplied, or the scope is
            unknown.
        """
        if scope == "system":
            return os.path.join(self._compiled_root, "system")
        if scope == "orchestrator":
            return os.path.join(self._compiled_root, "orchestrator")
        if scope == "agent_type":
            if not identifier:
                raise ValueError("'agent_type' scope requires an identifier")
            return os.path.join(self._compiled_root, "agent-types", identifier)
        if scope == "project":
            if not identifier:
                raise ValueError("'project' scope requires an identifier")
            return os.path.join(self._compiled_root, "projects", identifier)
        raise ValueError(f"Unknown scope: {scope!r}")

    def compiled_path(
        self,
        playbook_id: str,
        scope: Scope,
        identifier: str | None = None,
    ) -> str:
        """Return the absolute path where a compiled playbook would be stored.

        This is a pure path computation — does not touch the filesystem.

        Parameters
        ----------
        playbook_id:
            The playbook identifier (e.g. ``"task-outcome"``).
        scope:
            One of ``"system"``, ``"orchestrator"``, ``"agent_type"``, or
            ``"project"``.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        str
            Absolute filesystem path ending in ``{playbook_id}.compiled.json``.
        """
        return os.path.join(
            self._scope_dir(scope, identifier),
            f"{playbook_id}{COMPILED_SUFFIX}",
        )

    # ------------------------------------------------------------------
    # CRUD operations
    # ------------------------------------------------------------------

    def save(
        self,
        playbook: CompiledPlaybook,
        scope: Scope,
        identifier: str | None = None,
    ) -> str:
        """Serialize and write a compiled playbook to disk.

        Creates parent directories on-demand.  Overwrites any existing file
        for the same playbook ID and scope.

        Parameters
        ----------
        playbook:
            A validated :class:`CompiledPlaybook` instance.
        scope:
            Target scope (must match the playbook's own ``scope`` field for
            consistency, but this is not enforced — the caller is responsible).
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        str
            The absolute path the file was written to.

        Raises
        ------
        ValueError
            If the scope/identifier combination is invalid.
        OSError
            If the file cannot be written (permissions, disk full, etc.).
        """
        path = self.compiled_path(playbook.id, scope, identifier)

        # Ensure parent directory exists
        os.makedirs(os.path.dirname(path), exist_ok=True)

        data = playbook.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")  # trailing newline for POSIX compliance

        logger.info(
            "Saved compiled playbook '%s' (v%d, hash=%s) → %s",
            playbook.id,
            playbook.version,
            playbook.source_hash,
            path,
        )
        return path

    def load(
        self,
        playbook_id: str,
        scope: Scope,
        identifier: str | None = None,
    ) -> CompiledPlaybook | None:
        """Load a compiled playbook from disk.

        Parameters
        ----------
        playbook_id:
            The playbook identifier.
        scope:
            The scope to look in.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        CompiledPlaybook or None
            The deserialized playbook, or ``None`` if the file does not exist
            or cannot be parsed.
        """
        path = self.compiled_path(playbook_id, scope, identifier)

        if not os.path.isfile(path):
            logger.debug(
                "Compiled playbook '%s' not found at %s",
                playbook_id,
                path,
            )
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            playbook = CompiledPlaybook.from_dict(data)
            logger.debug(
                "Loaded compiled playbook '%s' (v%d, hash=%s) from %s",
                playbook.id,
                playbook.version,
                playbook.source_hash,
                path,
            )
            return playbook
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning(
                "Failed to load compiled playbook '%s' from %s: %s",
                playbook_id,
                path,
                exc,
            )
            return None

    def delete(
        self,
        playbook_id: str,
        scope: Scope,
        identifier: str | None = None,
    ) -> bool:
        """Delete a compiled playbook file from disk.

        Parameters
        ----------
        playbook_id:
            The playbook identifier.
        scope:
            The scope to delete from.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        bool
            ``True`` if the file existed and was deleted, ``False`` if it
            did not exist.
        """
        path = self.compiled_path(playbook_id, scope, identifier)

        if not os.path.isfile(path):
            logger.debug(
                "No compiled playbook '%s' to delete at %s",
                playbook_id,
                path,
            )
            return False

        os.remove(path)
        logger.info(
            "Deleted compiled playbook '%s' from %s",
            playbook_id,
            path,
        )
        return True

    def list_playbooks(
        self,
        scope: Scope,
        identifier: str | None = None,
    ) -> list[CompiledPlaybook]:
        """List all compiled playbooks in a given scope.

        Scans the scope directory for ``*.compiled.json`` files, deserializes
        each one, and returns valid playbooks.  Files that fail to parse are
        logged as warnings and skipped.

        Parameters
        ----------
        scope:
            The scope to list.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        list[CompiledPlaybook]
            All successfully loaded playbooks, sorted by ID for deterministic
            ordering.
        """
        scope_dir = self._scope_dir(scope, identifier)

        if not os.path.isdir(scope_dir):
            return []

        playbooks: list[CompiledPlaybook] = []
        for filename in sorted(os.listdir(scope_dir)):
            if not filename.endswith(COMPILED_SUFFIX):
                continue

            path = os.path.join(scope_dir, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                playbook = CompiledPlaybook.from_dict(data)
                playbooks.append(playbook)
            except (json.JSONDecodeError, KeyError, TypeError, OSError) as exc:
                logger.warning(
                    "Skipping malformed compiled playbook %s: %s",
                    path,
                    exc,
                )

        return playbooks

    def list_all(self) -> list[tuple[Scope, str | None, CompiledPlaybook]]:
        """List all compiled playbooks across all scopes.

        Walks the entire ``compiled/`` tree and returns each playbook
        annotated with its scope and identifier.

        Returns
        -------
        list[tuple[Scope, str | None, CompiledPlaybook]]
            Each tuple is ``(scope, identifier, playbook)``.  Sorted by
            scope then playbook ID.
        """
        results: list[tuple[Scope, str | None, CompiledPlaybook]] = []

        # Singleton scopes
        for scope in ("system", "orchestrator"):
            for pb in self.list_playbooks(scope):
                results.append((scope, None, pb))

        # agent-types/{type}/
        agent_types_dir = os.path.join(self._compiled_root, "agent-types")
        if os.path.isdir(agent_types_dir):
            for agent_type in sorted(os.listdir(agent_types_dir)):
                type_dir = os.path.join(agent_types_dir, agent_type)
                if os.path.isdir(type_dir):
                    for pb in self.list_playbooks("agent_type", agent_type):
                        results.append(("agent_type", agent_type, pb))

        # projects/{project_id}/
        projects_dir = os.path.join(self._compiled_root, "projects")
        if os.path.isdir(projects_dir):
            for project_id in sorted(os.listdir(projects_dir)):
                project_dir = os.path.join(projects_dir, project_id)
                if os.path.isdir(project_dir):
                    for pb in self.list_playbooks("project", project_id):
                        results.append(("project", project_id, pb))

        return results

    # ------------------------------------------------------------------
    # Change detection
    # ------------------------------------------------------------------

    def needs_recompile(
        self,
        playbook_id: str,
        source_hash: str,
        scope: Scope,
        identifier: str | None = None,
    ) -> bool:
        """Check whether a playbook needs recompilation.

        Compares the ``source_hash`` of the currently stored compiled version
        with the provided hash (typically computed from the current markdown
        source).  Returns ``True`` if:

        - No compiled version exists, or
        - The existing compiled version has a different source hash.

        This supports the change-detection workflow described in the spec §4:
        skip recompilation when the markdown hasn't changed.

        Parameters
        ----------
        playbook_id:
            The playbook identifier.
        source_hash:
            SHA-256 hash (16 hex chars) of the current markdown source.
        scope:
            Scope to check.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        bool
            ``True`` if recompilation is needed.
        """
        existing = self.load(playbook_id, scope, identifier)
        if existing is None:
            return True
        return existing.source_hash != source_hash

    def get_version(
        self,
        playbook_id: str,
        scope: Scope,
        identifier: str | None = None,
    ) -> int:
        """Return the version number of the currently stored compiled playbook.

        Used by the compiler to determine the next version number
        (``existing_version + 1``).

        Parameters
        ----------
        playbook_id:
            The playbook identifier.
        scope:
            Scope to check.
        identifier:
            Required for ``"agent_type"`` and ``"project"`` scopes.

        Returns
        -------
        int
            The current version number, or ``0`` if no compiled version exists.
        """
        existing = self.load(playbook_id, scope, identifier)
        return existing.version if existing is not None else 0
