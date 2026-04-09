"""Vault watcher handler for ``facts.md`` files — KV source-of-truth sync.

Registers glob patterns with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to ``facts.md`` files across all vault scopes (system,
orchestrator, agent-type, project).  When a facts file is created, modified, or
deleted, the handler parses the file and syncs the key-value entries to the
Milvus KV backend via :class:`~src.memory_v2_service.MemoryV2Service`.

Patterns registered (relative to vault root)::

    system/facts.md             — system scope
    orchestrator/facts.md       — orchestrator scope
    agent-types/*/facts.md      — per agent-type profile
    projects/*/facts.md         — per project

See ``docs/specs/design/memory-plugin.md`` Section 7 for the specification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.facts_parser import parse_facts_file

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

# Maps derive_scope() scope names to the scope strings expected by
# MemoryV2Service.kv_set().
_SCOPE_TO_KV_SCOPE: dict[str, str] = {
    "system": "system",
    "orchestrator": "orchestrator",
    # agent_type and project use identifier-qualified scope strings
}


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


def _scope_to_kv_scope(scope: str, identifier: str | None) -> str:
    """Convert a derive_scope() result to a kv_set-compatible scope string.

    Parameters
    ----------
    scope:
        The scope name from :func:`derive_scope` (e.g. ``"project"``).
    identifier:
        Scope-specific identifier (e.g. project id).

    Returns
    -------
    str
        Scope string for ``MemoryV2Service.kv_set(scope=...)``.
    """
    if scope == "system":
        return "system"
    if scope == "orchestrator":
        return "orchestrator"
    if scope == "agent_type" and identifier:
        return f"agenttype_{identifier}"
    if scope == "project" and identifier:
        return f"project_{identifier}"
    return scope


def _project_id_for_scope(scope: str, identifier: str | None) -> str:
    """Derive a project_id for kv_set calls.

    For project scopes, the identifier *is* the project id.
    For other scopes, use a synthetic placeholder (the service resolves
    the actual collection from the explicit scope parameter).
    """
    if scope == "project" and identifier:
        return identifier
    # Non-project scopes: use scope as synthetic project_id;
    # the explicit scope= override ensures the correct collection.
    return identifier or scope


async def _sync_facts_to_kv(
    facts_path: str,
    scope: str,
    identifier: str | None,
    service: Any,
) -> int:
    """Parse a facts.md file and upsert all entries to the KV backend.

    Parameters
    ----------
    facts_path:
        Absolute path to the facts.md file.
    scope:
        Memory scope from :func:`derive_scope`.
    identifier:
        Scope-specific identifier.
    service:
        A :class:`~src.memory_v2_service.MemoryV2Service` instance (or
        any object implementing the ``kv_set`` protocol).

    Returns
    -------
    int
        Number of KV entries synced.
    """
    path = Path(facts_path)
    if not path.exists():
        logger.warning("Facts file does not exist: %s", facts_path)
        return 0

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        logger.error("Could not read facts file: %s", facts_path, exc_info=True)
        return 0

    parsed = parse_facts_file(text)
    if not parsed:
        logger.debug("No KV entries parsed from %s", facts_path)
        return 0

    kv_scope = _scope_to_kv_scope(scope, identifier)
    project_id = _project_id_for_scope(scope, identifier)

    count = 0
    for namespace, entries in parsed.items():
        for key, value in entries.items():
            try:
                await service.kv_set(
                    project_id,
                    namespace,
                    key,
                    value,
                    scope=kv_scope,
                    _from_vault=True,
                )
                count += 1
            except Exception:
                logger.error(
                    "Failed to sync KV entry %s/%s from %s",
                    namespace,
                    key,
                    facts_path,
                    exc_info=True,
                )

    return count


async def on_facts_changed(
    changes: list[VaultChange],
    *,
    service: Any | None = None,
) -> None:
    """Handle changes to ``facts.md`` files in the vault.

    When a ``MemoryV2Service`` is available (passed via *service*), this
    handler parses the changed facts files and syncs their KV entries to
    the Milvus backend.  When no service is available, falls back to
    logging (Phase 1 stub behaviour).

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching one of the registered facts patterns.
    service:
        Optional :class:`~src.memory_v2_service.MemoryV2Service` instance.
        When provided, KV entries are synced to the backend.
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

        if service is None or not getattr(service, "available", False):
            # No service available — log-only fallback (Phase 1 behaviour).
            logger.info(
                "facts.md %s in scope %s: %s (service unavailable, skipping KV sync)",
                info.change_type,
                scope_label,
                info.file_path,
            )
            continue

        if info.change_type == "deleted":
            logger.info(
                "facts.md deleted in scope %s: %s (KV entries retained until explicit removal)",
                scope_label,
                info.file_path,
            )
            continue

        # created or modified — parse and sync
        logger.info(
            "facts.md %s in scope %s: %s — syncing KV entries",
            info.change_type,
            scope_label,
            info.file_path,
        )

        count = await _sync_facts_to_kv(
            info.file_path,
            info.scope,
            info.identifier,
            service,
        )

        logger.info(
            "Synced %d KV entries from facts.md in scope %s",
            count,
            scope_label,
        )


def register_facts_handlers(
    watcher: VaultWatcher,
    *,
    service: Any | None = None,
) -> list[str]:
    """Register facts-file watcher handlers on the given *watcher*.

    Registers one handler per pattern in :data:`FACTS_PATTERNS`.  All
    handlers share the same callback which parses the facts file and
    syncs entries to the KV backend when a *service* is provided.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to register
        handlers on (typically ``orchestrator.vault_watcher``).
    service:
        Optional :class:`~src.memory_v2_service.MemoryV2Service` instance.
        When provided, the handler will parse facts files and sync KV
        entries to the backend.  When ``None``, the handler falls back to
        logging only.

    Returns
    -------
    list[str]
        The handler IDs assigned by the watcher (one per pattern).
    """

    async def _handler(changes: list[VaultChange]) -> None:
        await on_facts_changed(changes, service=service)

    handler_ids: list[str] = []
    for pattern in FACTS_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            _handler,
            handler_id=f"facts:{pattern}",
        )
        handler_ids.append(hid)
        logger.debug("Registered facts handler for pattern %r (id=%s)", pattern, hid)

    logger.info(
        "Registered %d facts.md watcher handler(s) for KV sync%s",
        len(handler_ids),
        " (service connected)" if service else " (log-only, no service)",
    )
    return handler_ids
