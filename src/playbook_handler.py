"""Vault watcher handler for playbook ``.md`` files — compilation dispatch.

Registers glob patterns with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to playbook markdown files across all vault scopes (system,
orchestrator, agent-types, projects).  When a playbook file is created,
modified, or deleted, the handler logs the change for observability.

This is the **Phase 4 stub** — actual playbook compilation will be
implemented in Phase 5.  The handler currently logs changes at INFO level
so operators can verify watcher dispatch is working end-to-end.

Patterns registered (relative to vault root)::

    system/playbooks/*.md              — system-level playbooks
    orchestrator/playbooks/*.md        — orchestrator-level playbooks
    agent-types/*/playbooks/*.md       — per agent-type playbooks
    projects/*/playbooks/*.md          — per project playbooks

Matches examples:
    - vault/system/playbooks/deploy.md
    - vault/projects/my-app/playbooks/code-review.md
    - vault/agent-types/coding/playbooks/quality-gate.md
    - vault/orchestrator/playbooks/task-routing.md

See ``docs/specs/design/playbooks.md`` Section 17 for the specification.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob patterns for playbook files (relative to vault root).
#
# We use explicit scope prefixes rather than a single ``*/playbooks/*.md``
# pattern to avoid false matches on unexpected directory structures and to
# mirror the four vault scopes defined in the playbooks spec §17:
#   system, orchestrator, agent-types, projects
PLAYBOOK_PATTERNS: list[str] = [
    "system/playbooks/*.md",
    "orchestrator/playbooks/*.md",
    "agent-types/*/playbooks/*.md",
    "projects/*/playbooks/*.md",
]


def derive_playbook_scope(rel_path: str) -> tuple[str, str | None]:
    """Derive the playbook scope and identifier from a vault-relative path.

    Parameters
    ----------
    rel_path:
        Path relative to the vault root, e.g.
        ``projects/my-app/playbooks/deploy.md`` or
        ``system/playbooks/notify.md``.

    Returns
    -------
    tuple[str, str | None]
        A ``(scope, identifier)`` pair.  *identifier* is ``None`` for
        singleton scopes (``system``, ``orchestrator``).

    Examples
    --------
    >>> derive_playbook_scope("system/playbooks/deploy.md")
    ('system', None)
    >>> derive_playbook_scope("orchestrator/playbooks/routing.md")
    ('orchestrator', None)
    >>> derive_playbook_scope("agent-types/coding/playbooks/quality.md")
    ('agent_type', 'coding')
    >>> derive_playbook_scope("projects/my-app/playbooks/review.md")
    ('project', 'my-app')
    """
    parts = rel_path.replace("\\", "/").split("/")

    if len(parts) >= 3 and parts[0] == "projects":
        return "project", parts[1]

    if len(parts) >= 3 and parts[0] == "agent-types":
        return "agent_type", parts[1]

    if len(parts) >= 2:
        scope_name = parts[0]
        if scope_name in ("system", "orchestrator"):
            return scope_name, None

    return parts[0] if parts else "unknown", None


async def on_playbook_changed(changes: list[VaultChange]) -> None:
    """Handle changes to playbook ``.md`` files in the vault.

    This is the **Phase 4 stub handler**.  It derives the scope from each
    changed file's path and logs the event.  Actual playbook compilation
    (parsing the markdown into an executable graph) will be added in Phase 5.

    The handler receives the file path and change type (created/modified/
    deleted) for each matching file, as required by the task specification.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching one of the registered playbook patterns.
    """
    for change in changes:
        scope, identifier = derive_playbook_scope(change.rel_path)
        scope_label = f"{scope}/{identifier}" if identifier else scope
        logger.info(
            "Playbook %s in scope %s: %s (compilation pending Phase 5)",
            change.operation,
            scope_label,
            change.rel_path,
        )


def register_playbook_handlers(watcher: VaultWatcher) -> list[str]:
    """Register playbook-file watcher handlers on the given *watcher*.

    Registers one handler per pattern in :data:`PLAYBOOK_PATTERNS`.  All
    handlers share the same :func:`on_playbook_changed` callback.

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
    for pattern in PLAYBOOK_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            on_playbook_changed,
            handler_id=f"playbook:{pattern}",
        )
        handler_ids.append(hid)
        logger.debug("Registered playbook handler for pattern %r (id=%s)", pattern, hid)

    logger.info(
        "Registered %d playbook watcher handler(s) for compilation dispatch",
        len(handler_ids),
    )
    return handler_ids
