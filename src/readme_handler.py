"""Vault watcher handler for project ``README.md`` files — orchestrator summary dispatch.

Registers a glob pattern with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to ``README.md`` files under ``projects/*/`` directories.  When
a project README is created, modified, or deleted, the handler derives the
``project_id`` from the file path and logs the change.

This is the **Phase 5 stub** — actual orchestrator summary generation (re-read
README → update ``vault/orchestrator/memory/project-{id}.md``) will be
implemented in Phase 6.  The handler currently logs changes at INFO level so
operators can verify watcher dispatch is working end-to-end.

Pattern registered (relative to vault root)::

    projects/*/README.md    — per-project README files

See ``docs/specs/design/self-improvement.md`` Section 5 for the orchestrator
memory model, and ``docs/specs/design/playbooks.md`` Section 17 for the unified
watcher architecture.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob pattern for project README files (relative to vault root).
README_PATTERN: str = "projects/*/README.md"


@dataclass(frozen=True)
class ReadmeChangeInfo:
    """Parsed change event for a project README file.

    Attributes:
        file_path: Absolute filesystem path to the README file.
        change_type: One of ``"created"``, ``"modified"``, or ``"deleted"``.
        project_id: The project identifier derived from the vault path
            (the directory name under ``projects/``).
    """

    file_path: str
    change_type: str
    project_id: str


def derive_project_id(rel_path: str) -> str | None:
    """Derive the project ID from a vault-relative README path.

    Expects paths of the form ``projects/<project_id>/README.md``.
    Returns ``None`` if the path does not match the expected structure.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g.
        ``projects/my-app/README.md``.

    Returns
    -------
    str | None
        The project ID, or ``None`` if the path cannot be parsed.

    Examples
    --------
    >>> derive_project_id("projects/my-app/README.md")
    'my-app'
    >>> derive_project_id("projects/mech-fighters/README.md")
    'mech-fighters'
    >>> derive_project_id("system/README.md")
    """
    parts = rel_path.replace("\\", "/").split("/")

    if len(parts) == 3 and parts[0] == "projects" and parts[2] == "README.md":
        return parts[1]

    return None


async def on_readme_changed(changes: list[VaultChange]) -> None:
    """Handle project README.md file changes detected by the VaultWatcher.

    This is a **stub/no-op handler**.  It derives the ``project_id`` from
    each changed file's path and logs the event.  Actual orchestrator
    summary generation (re-read README → update orchestrator memory) will
    be added in Phase 6.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for
        README files that were created, modified, or deleted.
    """
    for change in changes:
        project_id = derive_project_id(change.rel_path)
        if project_id is None:
            logger.warning(
                "README handler: could not derive project_id from path: %s",
                change.rel_path,
            )
            continue

        info = ReadmeChangeInfo(
            file_path=change.path,
            change_type=change.operation,
            project_id=project_id,
        )
        logger.info(
            "README %s for project %s: %s (orchestrator summary pending Phase 6)",
            info.change_type,
            info.project_id,
            info.file_path,
        )


def register_readme_handlers(watcher: VaultWatcher) -> str:
    """Register the project README watcher handler on the given *watcher*.

    Registers a single handler for the :data:`README_PATTERN` with
    :func:`on_readme_changed` as the callback.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to register
        the handler on (typically ``orchestrator.vault_watcher``).

    Returns
    -------
    str
        The handler ID assigned by the watcher.
    """
    handler_id = watcher.register_handler(
        README_PATTERN,
        on_readme_changed,
        handler_id="readme:projects/*/README.md",
    )
    logger.info(
        "Registered README watcher handler for pattern %r (id=%s)",
        README_PATTERN,
        handler_id,
    )
    return handler_id
