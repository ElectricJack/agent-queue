"""Profile sync handler — watches profile.md files in the vault.

Registers with the :class:`~src.vault_watcher.VaultWatcher` to receive
notifications when ``profile.md`` files are created, modified, or deleted
in ``agent-types/*/`` or ``orchestrator/`` directories.

This is a stub handler for Phase 3 of the profiles roadmap (1.3.3).
Actual profile parsing and DB sync will be implemented in Phase 4.

Patterns watched:
    - ``agent-types/*/profile.md`` — per-agent-type profile definitions
    - ``orchestrator/profile.md`` — orchestrator-level profile

See ``docs/specs/design/profiles.md`` Section 3 for the sync model.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob patterns for profile files (relative to vault root)
PROFILE_PATTERNS: list[str] = [
    "agent-types/*/profile.md",
    "orchestrator/profile.md",
]


async def on_profile_changed(changes: list[VaultChange]) -> None:
    """Handle profile.md file changes detected by the VaultWatcher.

    This is a stub/no-op handler.  It logs the change for observability
    but does not yet parse or sync profiles to the database.  Actual
    sync logic will be added in Phase 4.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for
        profile files that were created, modified, or deleted.
    """
    for change in changes:
        logger.info(
            "Profile change detected: %s %s",
            change.operation,
            change.rel_path,
        )


def register_profile_handlers(watcher: VaultWatcher) -> list[str]:
    """Register profile.md path handlers with the VaultWatcher.

    Registers :func:`on_profile_changed` for each pattern in
    :data:`PROFILE_PATTERNS`.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to
        register handlers with.

    Returns
    -------
    list[str]
        The handler IDs returned by the watcher (for unregistration).
    """
    handler_ids = []
    for pattern in PROFILE_PATTERNS:
        hid = watcher.register_handler(pattern, on_profile_changed)
        handler_ids.append(hid)
        logger.debug("Registered profile handler for pattern %r: %s", pattern, hid)

    logger.info(
        "Profile sync: registered %d handler(s) for profile.md patterns",
        len(handler_ids),
    )
    return handler_ids
