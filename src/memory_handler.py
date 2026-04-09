"""Vault watcher handler for memory ``.md`` files — re-index dispatch.

Registers glob patterns with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to markdown files under ``memory/`` directories across all vault
scopes (system, orchestrator, agent-type, project).  When a memory file is
created, modified, or deleted, the handler derives the scope from the file path
and logs the change.

This is the **Phase 1 stub** — actual re-indexing (vector DB upsert/delete)
will be implemented in Phase 2/3.  The handler currently logs changes at INFO
level so operators can verify watcher dispatch is working end-to-end.

Patterns registered (relative to vault root)::

    system/memory/*.md               — system-wide knowledge
    orchestrator/memory/*.md         — orchestrator memory
    agent-types/*/memory/*.md        — per agent-type cross-project wisdom
    projects/*/memory/**/*.md        — per project knowledge (incl. subdirs)

The project pattern uses ``**`` because project memory directories contain
nested subdirectories (``knowledge/``, ``insights/``), while system,
orchestrator, and agent-type memory directories are flat.

See ``docs/specs/design/vault.md`` Section 5 for the vault structure
specification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob patterns for memory markdown files (relative to vault root).
#
# We use explicit prefixes rather than a single ``**/memory/**/*.md`` to
# avoid matching unexpected paths (e.g. a plugin or template that happens
# to have a memory/ directory).  Each pattern maps cleanly to a scope.
MEMORY_PATTERNS: list[str] = [
    "system/memory/*.md",
    "orchestrator/memory/*.md",
    "agent-types/*/memory/*.md",
    "projects/*/memory/**/*.md",
]


@dataclass(frozen=True)
class MemoryChangeInfo:
    """Parsed change event for a memory file.

    Attributes:
        file_path: Absolute filesystem path to the memory file.
        change_type: One of ``"created"``, ``"modified"``, or ``"deleted"``.
        scope: Memory scope — ``"system"``, ``"orchestrator"``,
            ``"agent_type"``, or ``"project"``.
        identifier: Scope-specific identifier (e.g. project id or agent-type
            name).  ``None`` for singleton scopes (system, orchestrator).
    """

    file_path: str
    change_type: str
    scope: str
    identifier: str | None


def derive_memory_scope(rel_path: str) -> tuple[str, str | None]:
    """Derive the memory scope and identifier from a vault-relative path.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g.
        ``projects/my-app/memory/knowledge/arch.md``
        or ``system/memory/global-conventions.md``.

    Returns
    -------
    tuple[str, str | None]
        A ``(scope, identifier)`` pair.  *identifier* is ``None`` for
        singleton scopes (``system``, ``orchestrator``).

    Examples
    --------
    >>> derive_memory_scope("system/memory/global-conventions.md")
    ('system', None)
    >>> derive_memory_scope("orchestrator/memory/project-notes.md")
    ('orchestrator', None)
    >>> derive_memory_scope("agent-types/coding/memory/async-patterns.md")
    ('agent_type', 'coding')
    >>> derive_memory_scope("projects/my-app/memory/knowledge/arch.md")
    ('project', 'my-app')
    """
    # Normalise separators
    parts = rel_path.replace("\\", "/").split("/")

    if len(parts) >= 4 and parts[0] == "projects":
        return "project", parts[1]

    if len(parts) >= 4 and parts[0] == "agent-types":
        return "agent_type", parts[1]

    # Singleton scopes: system/memory/*.md, orchestrator/memory/*.md
    if len(parts) >= 3:
        scope_name = parts[0]
        if scope_name in ("system", "orchestrator"):
            return scope_name, None

    # Fallback — use the first path segment as scope name
    return parts[0] if parts else "unknown", None


async def on_memory_changed(changes: list[VaultChange]) -> None:
    """Handle changes to memory ``.md`` files in the vault.

    This is the **Phase 1 stub handler**.  It derives the scope from each
    changed file's path and logs the event.  Actual re-indexing (vector DB
    upsert/delete) will be added in Phase 2/3.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching one of the registered memory patterns.
    """
    for change in changes:
        scope, identifier = derive_memory_scope(change.rel_path)
        info = MemoryChangeInfo(
            file_path=change.path,
            change_type=change.operation,
            scope=scope,
            identifier=identifier,
        )
        scope_label = f"{info.scope}/{info.identifier}" if info.identifier else info.scope
        logger.info(
            "memory.md %s in scope %s: %s (re-index pending Phase 2/3)",
            info.change_type,
            scope_label,
            info.file_path,
        )


def register_memory_handlers(watcher: VaultWatcher) -> list[str]:
    """Register memory-file watcher handlers on the given *watcher*.

    Registers one handler per pattern in :data:`MEMORY_PATTERNS`.  All
    handlers share the same :func:`on_memory_changed` callback.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to register
        handlers on (typically ``orchestrator.vault_watcher``).

    Returns
    -------
    list[str]
        The handler IDs assigned by the watcher (one per pattern).
    """
    handler_ids: list[str] = []
    for pattern in MEMORY_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            on_memory_changed,
            handler_id=f"memory:{pattern}",
        )
        handler_ids.append(hid)
        logger.debug("Registered memory handler for pattern %r (id=%s)", pattern, hid)

    logger.info(
        "Registered %d memory.md watcher handler(s) for re-index dispatch",
        len(handler_ids),
    )
    return handler_ids
