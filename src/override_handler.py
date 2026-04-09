"""Vault watcher handler for override ``.md`` files — re-index dispatch.

Registers a glob pattern with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to per-project agent-type override files.  When an override file
is created, modified, or deleted, the handler derives the ``project_id`` and
``agent_type`` from the file path and logs the change.

This is the **Phase 1 stub** — actual override indexing (injecting into agent
context, vector DB upsert/delete) will be implemented in Phase 3.  The handler
currently logs changes at INFO level so operators can verify watcher dispatch
is working end-to-end.

Pattern registered (relative to vault root)::

    projects/*/overrides/*.md       — per-project agent-type overrides

Path structure:  ``projects/{project_id}/overrides/{agent_type}.md``

The ``project_id`` is extracted from the second path segment (``projects/<here>/...``),
and the ``agent_type`` is extracted from the filename stem (e.g. ``coding.md`` →
``coding``).

See ``docs/specs/design/memory-scoping.md`` Section 5 for the override model
specification.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob pattern for override markdown files (relative to vault root).
OVERRIDE_PATTERN: str = "projects/*/overrides/*.md"


@dataclass(frozen=True)
class OverrideChangeInfo:
    """Parsed change event for an override file.

    Attributes:
        file_path: Absolute filesystem path to the override file.
        change_type: One of ``"created"``, ``"modified"``, or ``"deleted"``.
        project_id: Project identifier extracted from the path
            (e.g. ``"mech-fighters"``).
        agent_type: Agent type extracted from the filename stem
            (e.g. ``"coding"`` from ``coding.md``).
    """

    file_path: str
    change_type: str
    project_id: str
    agent_type: str


def derive_override_info(rel_path: str) -> tuple[str, str]:
    """Derive project_id and agent_type from a vault-relative override path.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g.
        ``projects/mech-fighters/overrides/coding.md``.

    Returns
    -------
    tuple[str, str]
        A ``(project_id, agent_type)`` pair.

    Raises
    ------
    ValueError
        If the path does not conform to the expected
        ``projects/{project_id}/overrides/{agent_type}.md`` structure.

    Examples
    --------
    >>> derive_override_info("projects/mech-fighters/overrides/coding.md")
    ('mech-fighters', 'coding')
    >>> derive_override_info("projects/my-app/overrides/review-specialist.md")
    ('my-app', 'review-specialist')
    """
    # Normalise separators
    parts = rel_path.replace("\\", "/").split("/")

    if (
        len(parts) >= 4
        and parts[0] == "projects"
        and parts[2] == "overrides"
        and parts[3].endswith(".md")
    ):
        project_id = parts[1]
        agent_type = os.path.splitext(parts[3])[0]  # strip .md extension
        return project_id, agent_type

    raise ValueError(
        f"Path does not match expected override structure "
        f"'projects/{{project_id}}/overrides/{{agent_type}}.md': {rel_path}"
    )


async def on_override_changed(changes: list[VaultChange]) -> None:
    """Handle changes to override ``.md`` files in the vault.

    This is the **Phase 1 stub handler**.  It derives the project_id and
    agent_type from each changed file's path and logs the event.  Actual
    override indexing (injecting into agent context) will be added in Phase 3.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching the override pattern.
    """
    for change in changes:
        try:
            project_id, agent_type = derive_override_info(change.rel_path)
        except ValueError:
            logger.warning(
                "override.md change with unexpected path structure: %s",
                change.rel_path,
            )
            continue

        info = OverrideChangeInfo(
            file_path=change.path,
            change_type=change.operation,
            project_id=project_id,
            agent_type=agent_type,
        )
        logger.info(
            "override.md %s for project=%s agent_type=%s: %s (indexing pending Phase 3)",
            info.change_type,
            info.project_id,
            info.agent_type,
            info.file_path,
        )


def register_override_handlers(watcher: VaultWatcher) -> str:
    """Register the override-file watcher handler on the given *watcher*.

    Registers a single handler for the :data:`OVERRIDE_PATTERN`.

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
    hid = watcher.register_handler(
        OVERRIDE_PATTERN,
        on_override_changed,
        handler_id=f"override:{OVERRIDE_PATTERN}",
    )
    logger.debug("Registered override handler for pattern %r (id=%s)", OVERRIDE_PATTERN, hid)

    logger.info(
        "Registered override.md watcher handler for re-index dispatch",
    )
    return hid
