"""Vault watcher handler for playbook ``.md`` files — compilation dispatch.

Registers glob patterns with the :class:`~src.vault_watcher.VaultWatcher` to
detect changes to playbook markdown files across all vault scopes (system,
supervisor, agent-types, projects).  When a playbook file is created or
modified, the handler triggers compilation via :class:`PlaybookManager`.
When a playbook file is deleted, the compiled version is removed from the
active registry.

Patterns registered (relative to vault root)::

    system/playbooks/*.md              — system-level playbooks
    agent-types/supervisor/playbooks/*.md — supervisor-level playbooks
    agent-types/*/playbooks/*.md       — per agent-type playbooks
    projects/*/playbooks/*.md          — per project playbooks

Matches examples:
    - vault/system/playbooks/deploy.md
    - vault/projects/my-app/playbooks/code-review.md
    - vault/agent-types/coding/playbooks/quality-gate.md
    - vault/agent-types/supervisor/playbooks/task-routing.md

See ``docs/specs/design/playbooks.md`` Section 17 for the specification.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from src.playbooks.manager import PlaybookManager
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)


def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Best-effort YAML-frontmatter split (mirrors PlaybookCompiler helper).

    Kept local to this module so the vault watcher can peek at ``kind`` /
    ``id`` without importing the compiler (Phase 6 removes the LLM
    compiler code path entirely).
    """
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}, content
    return meta, parts[2]

# Glob patterns for playbook files (relative to vault root).
#
# We use explicit scope prefixes rather than a single ``*/playbooks/*.md``
# pattern to avoid false matches on unexpected directory structures and to
# mirror the vault scopes defined in the playbooks spec §17:
#   system, agent-types (including supervisor), projects
# The supervisor scope is covered by the agent-types/*/playbooks/*.md pattern.
PLAYBOOK_PATTERNS: list[str] = [
    "system/playbooks/*.md",
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
        singleton scopes (``system``, ``supervisor``).

    Examples
    --------
    >>> derive_playbook_scope("system/playbooks/deploy.md")
    ('system', None)
    >>> derive_playbook_scope("agent-types/supervisor/playbooks/routing.md")
    ('supervisor', None)
    >>> derive_playbook_scope("agent-types/coding/playbooks/quality.md")
    ('agent_type', 'coding')
    >>> derive_playbook_scope("projects/my-app/playbooks/review.md")
    ('project', 'my-app')
    """
    parts = rel_path.replace("\\", "/").split("/")

    if len(parts) >= 3 and parts[0] == "projects":
        return "project", parts[1]

    if len(parts) >= 3 and parts[0] == "agent-types":
        # The supervisor is a special agent-type that acts as a singleton scope
        if parts[1] == "supervisor":
            return "supervisor", None
        return "agent_type", parts[1]

    if len(parts) >= 2:
        scope_name = parts[0]
        if scope_name == "system":
            return scope_name, None

    return parts[0] if parts else "unknown", None


def _derive_playbook_id_from_path(rel_path: str) -> str | None:
    """Extract the playbook filename stem as a fallback ID.

    Parameters
    ----------
    rel_path:
        Vault-relative path (e.g. ``system/playbooks/deploy.md``).

    Returns
    -------
    str | None
        The filename without extension (e.g. ``"deploy"``), or ``None``
        if the path has no recognizable filename.
    """
    parts = rel_path.replace("\\", "/").split("/")
    if parts and parts[-1].endswith(".md"):
        return parts[-1][:-3]
    return None


async def on_playbook_changed(
    changes: list[VaultChange],
    *,
    playbook_manager: PlaybookManager | None = None,
) -> None:
    """Handle changes to playbook ``.md`` files in the vault.

    For ``created`` and ``modified`` operations, reads the file and triggers
    compilation via the :class:`PlaybookManager`.  If compilation fails, the
    previous compiled version remains active and an error notification is
    emitted by the manager.

    For ``deleted`` operations, removes the playbook from the active registry
    using the filename stem as the playbook ID.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for files
        matching one of the registered playbook patterns.
    playbook_manager:
        Optional :class:`~src.playbooks.manager.PlaybookManager` instance.
        When ``None``, the handler falls back to log-only mode.
    """
    for change in changes:
        scope, identifier = derive_playbook_scope(change.rel_path)
        scope_label = f"{scope}/{identifier}" if identifier else scope

        if playbook_manager is None:
            logger.info(
                "Playbook %s in scope %s: %s (no manager, skipping compilation)",
                change.operation,
                scope_label,
                change.rel_path,
            )
            continue

        if change.operation == "deleted":
            # Prefer looking up the playbook id by source-path (exact
            # match against ``_source_paths``) — this works even when the
            # deleted file's frontmatter id differed from its filename
            # stem.  Fall back to the filename-stem heuristic if the
            # source-path isn't tracked (e.g. manager was restarted
            # between compile and delete).
            path_id = playbook_manager.playbook_id_by_source_path(change.path)
            fallback_id = _derive_playbook_id_from_path(change.rel_path)
            target_id = path_id or fallback_id
            if target_id:
                removed = await playbook_manager.remove_playbook(target_id)
                if removed:
                    logger.info(
                        "Playbook deleted in scope %s: %s (removed '%s' from registry)",
                        scope_label,
                        change.rel_path,
                        target_id,
                    )
                else:
                    logger.info(
                        "Playbook deleted in scope %s: %s (id '%s' not in registry)",
                        scope_label,
                        change.rel_path,
                        target_id,
                    )
            else:
                logger.warning(
                    "Playbook deleted in scope %s: %s (could not derive ID)",
                    scope_label,
                    change.rel_path,
                )
            continue

        # created or modified — inspect frontmatter to route
        try:
            markdown = Path(change.path).read_text(encoding="utf-8")
        except OSError:
            logger.error(
                "Could not read playbook file: %s",
                change.path,
                exc_info=True,
            )
            continue

        fm, _body = _parse_frontmatter(markdown)
        playbook_id = fm.get("id") or _derive_playbook_id_from_path(change.rel_path)

        # Deterministic-parse route for kind: pipeline (Phase 1).
        if fm.get("kind") == "pipeline":
            logger.info(
                "Compiling pipeline playbook %s (scope=%s, deterministic parse)",
                change.rel_path,
                scope_label,
            )
            result = await playbook_manager.compile_playbook_pipeline(
                markdown,
                source_path=change.path,
                rel_path=change.rel_path,
                scope_identifier=identifier,
            )
            if result.success:
                logger.info(
                    "Pipeline playbook compiled: %s (scope=%s)",
                    change.rel_path,
                    scope_label,
                )
            else:
                logger.warning(
                    "Pipeline compile failed for %s (scope=%s): %s",
                    change.rel_path,
                    scope_label,
                    "; ".join(result.errors),
                )
            continue

        # Ordinary playbook: enqueue a compile task (Phase 6, compiler-as-agent).
        if playbook_id is None:
            logger.warning(
                "Cannot enqueue compile task — no id derivable from %s",
                change.rel_path,
            )
            continue

        handler = getattr(playbook_manager, "command_handler", None)
        if handler is None:
            logger.warning(
                "No command_handler wired on PlaybookManager — cannot enqueue "
                "compile task for %s",
                change.rel_path,
            )
            continue

        project_id = await playbook_manager.compile_task_project_id(scope, identifier)
        if project_id is None:
            logger.warning(
                "No project_id available to attach compile task for %s",
                change.rel_path,
            )
            continue

        dedup_key = f"playbook-compile:{playbook_id}"
        # ensure_task is the framework's find-or-create by dedup_key;
        # non-terminal existing tasks with the same key are returned as-is,
        # so touching the source markdown repeatedly does not spawn duplicates.
        description = (
            f"Compile the markdown playbook at {change.path} into JSON.\n\n"
            "Steps:\n"
            "1. Read the markdown.\n"
            "2. Produce a compiled JSON artifact matching the schema.\n"
            "3. Call `playbook_validate(path=<your.json>)` and iterate on errors.\n"
            "4. When it validates, call "
            f"`playbook_install(playbook_id={playbook_id!r}, "
            "compiled_path=<your.json>)`.\n"
        )
        result = await handler.execute(
            "ensure_task",
            {
                "project_id": project_id,
                "title": f"Compile playbook {playbook_id}",
                "description": description,
                "dedup_key": dedup_key,
                "profile_id": "playbook-compiler",
            },
        )
        if not result.get("success"):
            logger.warning(
                "Failed to enqueue compile task for %s: %s",
                change.rel_path,
                result.get("error"),
            )
        else:
            logger.info(
                "Enqueued compile task for playbook %s (scope=%s, dedup=%s, created=%s)",
                playbook_id,
                scope_label,
                dedup_key,
                result.get("created"),
            )


def register_playbook_handlers(
    watcher: VaultWatcher,
    *,
    playbook_manager: PlaybookManager | None = None,
) -> list[str]:
    """Register playbook-file watcher handlers on the given *watcher*.

    Registers one handler per pattern in :data:`PLAYBOOK_PATTERNS`.  All
    handlers share the same :func:`on_playbook_changed` callback.

    When a *playbook_manager* is provided, detected changes trigger actual
    compilation.  Without it, the handler falls back to log-only mode.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to register
        handlers on (typically ``orchestrator.vault_watcher``).
    playbook_manager:
        Optional :class:`~src.playbooks.manager.PlaybookManager` instance.
        When provided, file changes trigger compilation and version management.

    Returns
    -------
    list[str]
        The handler IDs assigned by the watcher (one per pattern).
    """

    async def _handler(changes: list[VaultChange]) -> None:
        await on_playbook_changed(changes, playbook_manager=playbook_manager)

    handler_ids: list[str] = []
    for pattern in PLAYBOOK_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            _handler,
            handler_id=f"playbook:{pattern}",
        )
        handler_ids.append(hid)
        logger.debug("Registered playbook handler for pattern %r (id=%s)", pattern, hid)

    mode = "compilation" if playbook_manager else "log-only"
    logger.info(
        "Registered %d playbook watcher handler(s) (%s mode)",
        len(handler_ids),
        mode,
    )
    return handler_ids
