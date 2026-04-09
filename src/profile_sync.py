"""Profile sync — parsed markdown profiles to agent_profiles table upsert.

Implements the sync model from ``docs/specs/design/profiles.md`` Section 3:
markdown file -> parse -> validate -> upsert DB row.

The core function :func:`sync_profile_to_db` takes a :class:`ParsedProfile`
(produced by :func:`~src.profile_parser.parse_profile`) and upserts it into
the ``agent_profiles`` table.  It is called by :func:`on_profile_changed`
when the :class:`~src.vault_watcher.VaultWatcher` detects profile.md file
changes, and can also be called directly for programmatic sync.

Validation on sync (per spec Section 3):
- JSON blocks must parse successfully; if not, sync fails and the previous
  DB config remains active.
- Tool names in ``## Tools`` are soft-validated against the known tool
  registry.  Unknown tools produce warnings (not hard failures).
- MCP server commands are validated for basic structure (command exists,
  args are strings).

Patterns watched:
    - ``agent-types/*/profile.md`` -- per-agent-type profile definitions
    - ``orchestrator/profile.md`` -- orchestrator-level profile

See ``docs/specs/design/profiles.md`` Section 3 for the full sync model.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models import AgentProfile
from src.profile_parser import ParsedProfile, parse_profile, parsed_profile_to_agent_profile

if TYPE_CHECKING:
    from src.event_bus import EventBus
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob patterns for profile files (relative to vault root)
PROFILE_PATTERNS: list[str] = [
    "agent-types/*/profile.md",
    "orchestrator/profile.md",
]


@dataclass(frozen=True)
class ProfileSyncResult:
    """Result of a profile-to-DB sync operation.

    Attributes:
        success: Whether the sync completed without errors.
        action: What happened -- ``"created"``, ``"updated"``, or ``"none"``
            (when sync was skipped due to errors).
        profile_id: The profile ID that was synced (empty on failure).
        name: The profile display name (empty on failure).
        warnings: Non-fatal issues (e.g. unrecognized tool names).
        errors: Fatal issues that prevented sync.
    """

    success: bool
    action: str  # "created" | "updated" | "none"
    profile_id: str = ""
    name: str = ""
    warnings: list[str] | None = None
    errors: list[str] | None = None

    def to_dict(self) -> dict:
        """Convert to a dict suitable for command handler responses."""
        d: dict[str, Any] = {
            "success": self.success,
            "action": self.action,
        }
        if self.profile_id:
            d["profile_id"] = self.profile_id
        if self.name:
            d["name"] = self.name
        if self.warnings:
            d["warnings"] = self.warnings
        if self.errors:
            d["errors"] = self.errors
        return d


def derive_profile_id(rel_path: str) -> str | None:
    """Derive a profile ID from a vault-relative path.

    Used as a fallback when the profile markdown lacks a frontmatter ``id``
    field.  The ID is extracted from the directory structure:

    - ``agent-types/coding/profile.md`` -> ``"coding"``
    - ``orchestrator/profile.md`` -> ``"orchestrator"``

    Parameters
    ----------
    rel_path:
        Path relative to the vault root (e.g. ``agent-types/coding/profile.md``).

    Returns
    -------
    str | None
        The derived profile ID, or ``None`` if the path doesn't match
        a recognized profile location.
    """
    parts = rel_path.replace("\\", "/").split("/")

    # agent-types/<type>/profile.md -> <type>
    if len(parts) >= 3 and parts[0] == "agent-types" and parts[-1] == "profile.md":
        return parts[1]

    # orchestrator/profile.md -> "orchestrator"
    if len(parts) >= 2 and parts[0] == "orchestrator" and parts[-1] == "profile.md":
        return "orchestrator"

    return None


async def sync_profile_to_db(
    parsed: ParsedProfile,
    db: Any,
    *,
    source_path: str = "",
    fallback_id: str | None = None,
) -> ProfileSyncResult:
    """Sync a parsed profile into the agent_profiles database table.

    This is the core upsert function for the profile sync model (spec Section 3).
    Given a :class:`ParsedProfile` from :func:`~src.profile_parser.parse_profile`,
    it validates the result, converts to an :class:`~src.models.AgentProfile`,
    and creates or updates the corresponding database row.

    Parameters
    ----------
    parsed:
        The parsed profile from :func:`~src.profile_parser.parse_profile`.
    db:
        A database instance implementing the ``DatabaseBackend`` protocol
        (must support ``get_profile``, ``upsert_profile``).
    source_path:
        Optional path to the source file (for logging/diagnostics).
    fallback_id:
        Optional fallback profile ID to use when the frontmatter lacks an
        ``id`` field.  Typically derived from the vault path via
        :func:`derive_profile_id`.

    Returns
    -------
    ProfileSyncResult
        The sync outcome including success/failure, action taken, and any
        warnings or errors.
    """
    # 1. Check for parse errors -- abort sync if profile is invalid.
    #    Per spec: "JSON blocks must parse successfully; if not, sync fails
    #    and the previous DB config remains active."
    if not parsed.is_valid:
        return ProfileSyncResult(
            success=False,
            action="none",
            errors=parsed.errors,
        )

    # 2. Convert parsed profile to AgentProfile-compatible dict.
    profile_dict = parsed_profile_to_agent_profile(parsed)

    # 3. Resolve profile ID: frontmatter > fallback > error.
    profile_id = profile_dict.get("id") or fallback_id
    if not profile_id:
        return ProfileSyncResult(
            success=False,
            action="none",
            errors=[
                "Profile ID is required (set 'id' in frontmatter or use a recognized vault path)"
            ],
        )

    # Use profile ID as display name fallback.
    name = profile_dict.get("name") or profile_id

    # 4. Build AgentProfile instance with all fields.
    profile = AgentProfile(
        id=profile_id,
        name=name,
        description=profile_dict.get("description", ""),
        model=profile_dict.get("model", ""),
        permission_mode=profile_dict.get("permission_mode", ""),
        allowed_tools=profile_dict.get("allowed_tools", []),
        mcp_servers=profile_dict.get("mcp_servers", {}),
        system_prompt_suffix=profile_dict.get("system_prompt_suffix", ""),
        install=profile_dict.get("install", {}),
    )

    # 5. Soft-validate tool names (warnings, not errors -- per spec).
    warnings: list[str] = []
    if profile.allowed_tools:
        from src.known_tools import validate_tool_names

        unknown = validate_tool_names(profile.allowed_tools)
        if unknown:
            warnings.append(f"Unrecognized tools (will still be set): {', '.join(unknown)}")

    # 6. Upsert into database.
    try:
        action = await db.upsert_profile(profile)
    except Exception as exc:
        logger.error(
            "Database error during profile sync for '%s': %s",
            profile_id,
            exc,
            exc_info=True,
        )
        return ProfileSyncResult(
            success=False,
            action="none",
            profile_id=profile_id,
            name=name,
            errors=[f"Database error: {exc}"],
        )

    logger.info(
        "Profile sync %s: id=%s name=%r%s%s",
        action,
        profile_id,
        name,
        f" source={source_path}" if source_path else "",
        f" warnings={warnings}" if warnings else "",
    )

    return ProfileSyncResult(
        success=True,
        action=action,
        profile_id=profile_id,
        name=name,
        warnings=warnings if warnings else None,
    )


async def sync_profile_text_to_db(
    text: str,
    db: Any,
    *,
    source_path: str = "",
    fallback_id: str | None = None,
) -> ProfileSyncResult:
    """Parse markdown text and sync the resulting profile to the database.

    Convenience wrapper that combines :func:`~src.profile_parser.parse_profile`
    with :func:`sync_profile_to_db` in a single call.

    Parameters
    ----------
    text:
        Raw markdown content of a profile.md file.
    db:
        Database instance (see :func:`sync_profile_to_db`).
    source_path:
        Optional path for logging.
    fallback_id:
        Optional fallback profile ID.

    Returns
    -------
    ProfileSyncResult
        The sync outcome.
    """
    parsed = parse_profile(text)
    return await sync_profile_to_db(
        parsed,
        db,
        source_path=source_path,
        fallback_id=fallback_id,
    )


async def _emit_sync_failed(
    event_bus: EventBus | None,
    result: ProfileSyncResult,
    source_path: str,
) -> None:
    """Emit a ``notify.profile_sync_failed`` event if an event bus is available."""
    if event_bus is None:
        return
    try:
        from src.notifications.events import ProfileSyncFailedEvent

        event = ProfileSyncFailedEvent(
            profile_id=result.profile_id,
            source_path=source_path,
            errors=result.errors or [],
            warnings=result.warnings or [],
        )
        await event_bus.emit(event.event_type, event.model_dump(mode="json"))
    except Exception:
        logger.debug("Failed to emit profile sync notification", exc_info=True)


async def on_profile_changed(
    changes: list[VaultChange],
    *,
    db: Any | None = None,
    event_bus: EventBus | None = None,
) -> None:
    """Handle profile.md file changes detected by the VaultWatcher.

    When a database is available (passed via *db*), this handler reads
    the changed profile files, parses them, and syncs the result to the
    ``agent_profiles`` table.  When no database is available, falls back
    to logging only.

    For ``deleted`` operations, the handler logs the deletion but does
    **not** remove the profile from the database.  Per spec, the DB
    retains the last-known config until explicitly removed via command.

    Parameters
    ----------
    changes:
        List of :class:`~src.vault_watcher.VaultChange` objects for
        profile files that were created, modified, or deleted.
    db:
        Optional database instance.  When ``None``, handler falls back
        to log-only mode.
    event_bus:
        Optional :class:`~src.event_bus.EventBus` instance.  When
        provided, a ``notify.profile_sync_failed`` event is emitted
        on sync failure so that transport handlers (Discord, etc.)
        can alert the user.
    """
    for change in changes:
        path_id = derive_profile_id(change.rel_path)

        if db is None:
            logger.info(
                "Profile change detected: %s %s (no DB, skipping sync)",
                change.operation,
                change.rel_path,
            )
            continue

        if change.operation == "deleted":
            # Per spec: DB retains last-known config.  Deletion from DB
            # requires an explicit delete_profile command.
            logger.info(
                "Profile file deleted: %s (id=%s) — DB row retained",
                change.rel_path,
                path_id or "unknown",
            )
            continue

        # created or modified — read, parse, and sync
        try:
            text = Path(change.path).read_text(encoding="utf-8")
        except OSError:
            logger.error(
                "Could not read profile file: %s",
                change.path,
                exc_info=True,
            )
            continue

        result = await sync_profile_to_db(
            parse_profile(text),
            db,
            source_path=change.path,
            fallback_id=path_id,
        )

        if result.success:
            logger.info(
                "Profile sync %s via watcher: %s (id=%s)%s",
                result.action,
                change.rel_path,
                result.profile_id,
                f" warnings: {result.warnings}" if result.warnings else "",
            )
        else:
            # Sync failed — previous DB config remains active (per spec).
            # Emit a notification so transport handlers can alert the user.
            logger.error(
                "Profile sync failed for %s: %s",
                change.rel_path,
                result.errors,
            )
            await _emit_sync_failed(event_bus, result, change.rel_path)


def register_profile_handlers(
    watcher: VaultWatcher,
    *,
    db: Any | None = None,
    event_bus: EventBus | None = None,
) -> list[str]:
    """Register profile.md path handlers with the VaultWatcher.

    Registers :func:`on_profile_changed` for each pattern in
    :data:`PROFILE_PATTERNS`.  Uses deterministic handler IDs
    (``profile:<pattern>``) so re-registration with a new *db*
    replaces existing handlers.

    Parameters
    ----------
    watcher:
        The :class:`~src.vault_watcher.VaultWatcher` instance to
        register handlers with.
    db:
        Optional database instance.  When provided, detected profile
        changes are synced to the ``agent_profiles`` table.  When
        ``None``, the handler falls back to logging only.
    event_bus:
        Optional :class:`~src.event_bus.EventBus` instance.  When
        provided, a ``notify.profile_sync_failed`` event is emitted
        on sync failure.

    Returns
    -------
    list[str]
        The handler IDs returned by the watcher (for unregistration).
    """

    async def _handler(changes: list[VaultChange]) -> None:
        await on_profile_changed(changes, db=db, event_bus=event_bus)

    handler_ids: list[str] = []
    for pattern in PROFILE_PATTERNS:
        hid = watcher.register_handler(
            pattern,
            _handler,
            handler_id=f"profile:{pattern}",
        )
        handler_ids.append(hid)
        logger.debug("Registered profile handler for pattern %r (id=%s)", pattern, hid)

    logger.info(
        "Profile sync: registered %d handler(s) for profile.md patterns%s",
        len(handler_ids),
        " (DB connected)" if db else " (log-only, no DB)",
    )
    return handler_ids
