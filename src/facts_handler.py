"""Vault watcher handler for ``facts.md`` files — KV source-of-truth sync.

Registers glob patterns with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to ``facts.md`` files across all vault scopes (system,
orchestrator, agent-type, project).  When a facts file is created, modified, or
deleted, the handler derives the scope from the file path and logs the change.

This is the **Phase 1 stub** — actual KV sync to the Milvus backend is
implemented in Phase 2.  The handler currently logs changes at INFO level
so operators can verify watcher dispatch is working end-to-end.

Patterns registered (relative to vault root)::

    */facts.md                  — system, orchestrator
    agent-types/*/facts.md      — per agent-type profile
    projects/*/facts.md         — per project

See ``docs/specs/design/memory-plugin.md`` Section 7 for the specification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob patterns for facts files (relative to vault root).
#
# Note: we use literal paths for singleton scopes (system, orchestrator)
# rather than ``*/facts.md`` because Python's ``fnmatch.fnmatch`` matches
# ``*`` across path separators — ``*/facts.md`` would also match
# ``projects/app/facts.md``, causing double dispatch.  Literal patterns
# avoid the overlap while covering the exact same files.
FACTS_PATTERNS: list[str] = [
    "system/facts.md",
    "orchestrator/facts.md",
    "agent-types/*/facts.md",
    "projects/*/facts.md",
]


@dataclass(frozen=True)
class FactsChangeInfo:
    """Parsed change event for a facts file.

    Attributes:
        file_path: Absolute filesystem path to the facts file.
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


def derive_scope(rel_path: str) -> tuple[str, str | None]:
    """Derive the memory scope and identifier from a vault-relative path.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g. ``projects/my-app/facts.md``
        or ``system/facts.md``.

    Returns
    -------
    tuple[str, str | None]
        A ``(scope, identifier)`` pair.  *identifier* is ``None`` for
        singleton scopes (``system``, ``orchestrator``).

    Examples
    --------
    >>> derive_scope("system/facts.md")
    ('system', None)
    >>> derive_scope("orchestrator/facts.md")
    ('orchestrator', None)
    >>> derive_scope("agent-types/coding/facts.md")
    ('agent_type', 'coding')
    >>> derive_scope("projects/my-app/facts.md")
    ('project', 'my-app')
    """
    # Normalise separators
    parts = rel_path.replace("\\", "/").split("/")

    if len(parts) >= 3 and parts[0] == "projects":
        return "project", parts[1]

    if len(parts) >= 3 and parts[0] == "agent-types":
        return "agent_type", parts[1]

    # Singleton scopes: system/facts.md, orchestrator/facts.md
    if len(parts) >= 2:
        scope_name = parts[0]
        if scope_name in ("system", "orchestrator"):
            return scope_name, None

    # Fallback — use the first path segment as scope name
    return parts[0] if parts else "unknown", None


async def on_facts_changed(changes: list[VaultChange]) -> None:
    """Handle changes to ``facts.md`` files in the vault.

    This is the **Phase 1 stub handler**.  It derives the scope from each
    changed file's path and logs the event.  Actual KV sync (parsing the
    markdown, updating Milvus entries) will be added in Phase 2.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching one of the registered facts patterns.
    """
    for change in changes:
        scope, identifier = derive_scope(change.rel_path)
        info = FactsChangeInfo(
            file_path=change.path,
            change_type=change.operation,
            scope=scope,
            identifier=identifier,
        )
        scope_label = f"{info.scope}/{info.identifier}" if info.identifier else info.scope
        logger.info(
            "facts.md %s in scope %s: %s (KV sync pending Phase 2)",
            info.change_type,
            scope_label,
            info.file_path,
        )


def register_facts_handlers(watcher: VaultWatcher) -> list[str]:
    """Register facts-file watcher handlers on the given *watcher*.

    Registers one handler per pattern in :data:`FACTS_PATTERNS`.  All
    handlers share the same :func:`on_facts_changed` callback.

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
    for pattern in FACTS_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            on_facts_changed,
            handler_id=f"facts:{pattern}",
        )
        handler_ids.append(hid)
        logger.debug("Registered facts handler for pattern %r (id=%s)", pattern, hid)

    logger.info(
        "Registered %d facts.md watcher handler(s) for KV sync",
        len(handler_ids),
    )
    return handler_ids
