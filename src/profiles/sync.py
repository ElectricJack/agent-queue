"""Profile sync — parsed markdown profiles to agent_profiles table upsert.

Implements the sync model from ``docs/specs/design/profiles.md`` Section 3:
markdown file -> parse -> validate -> upsert DB row.

The core function :func:`sync_profile_to_db` takes a :class:`ParsedProfile`
(produced by :func:`~src.profiles.parser.parse_profile`) and upserts it into
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
    - ``agent-types/*/profile.md`` -- per-agent-type profile definitions (includes supervisor)
    - ``projects/*/agent-types/*/profile.md`` -- per-project agent-type overrides

See ``docs/specs/design/profiles.md`` Section 3 for the full sync model.
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.models import AgentProfile
from src.profiles.parser import ParsedProfile, parse_profile, parsed_profile_to_agent_profile

if TYPE_CHECKING:
    from src.event_bus import EventBus
    from src.vault_watcher import VaultChange, VaultWatcher

logger = logging.getLogger(__name__)

# Glob patterns for profile files (relative to vault root).  The legacy
# ``orchestrator/profile.md`` path was retired — ``supervisor`` is now
# the singleton scope.  If stale files remain on disk the vault
# structure migration handles them; we no longer match the pattern so
# the profile row can't be resurrected via vault sync.
PROFILE_PATTERNS: list[str] = [
    "agent-types/*/profile.md",
    "projects/*/agent-types/*/profile.md",
]


def project_scoped_profile_id(project_id: str, agent_type: str) -> str:
    """Format the scoped profile id for a per-project agent-type profile."""
    return f"project:{project_id}:{agent_type}"


def underlying_agent_type(profile_id: str | None) -> str | None:
    """Extract the underlying agent-type segment from a profile id.

    Project-scoped profile ids use the ``project:{project_id}:{type}``
    form; callers that emit agent-type metadata on events (e.g. the
    task executor passing ``agent_type`` on ``task.completed`` /
    ``task.failed``) must strip the scoping prefix so downstream
    consumers (the memory extractor, schedulers) see a plain agent-type
    string rather than the full profile id.

    For a non-scoped id (e.g. ``"claude-code"``), the input is
    returned unchanged.  For ``None`` or the empty string, returns
    ``None``.
    """
    if not profile_id:
        return None
    if profile_id.startswith("project:"):
        # Expected format: project:<project_id>:<agent_type>
        parts = profile_id.split(":", 2)
        if len(parts) == 3 and parts[2]:
            return parts[2]
    return profile_id


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
    - ``agent-types/supervisor/profile.md`` -> ``"supervisor"``

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

    # projects/<project>/agent-types/<type>/profile.md -> project:<project>:<type>
    if (
        len(parts) == 5
        and parts[0] == "projects"
        and parts[2] == "agent-types"
        and parts[-1] == "profile.md"
    ):
        return project_scoped_profile_id(parts[1], parts[3])

    # agent-types/<type>/profile.md -> <type>
    if len(parts) >= 3 and parts[0] == "agent-types" and parts[-1] == "profile.md":
        return parts[1]

    # Legacy ``orchestrator/profile.md`` path was retired (supervisor is
    # the singleton scope now).  Return None so any stale file there
    # can't produce a DB row.

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
    Given a :class:`ParsedProfile` from :func:`~src.profiles.parser.parse_profile`,
    it validates the result, converts to an :class:`~src.models.AgentProfile`,
    and creates or updates the corresponding database row.

    Parameters
    ----------
    parsed:
        The parsed profile from :func:`~src.profiles.parser.parse_profile`.
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
        memory_scope_id=profile_dict.get("memory_scope_id"),
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

    Convenience wrapper that combines :func:`~src.profiles.parser.parse_profile`
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
    data_dir: str | None = None,
) -> None:
    """Handle profile.md file changes detected by the VaultWatcher.

    When a database is available (passed via *db*), this handler reads
    the changed profile files, parses them, and syncs the result to the
    ``agent_profiles`` table.  When no database is available, falls back
    to logging only.

    For ``created`` operations on agent-type profiles, starter knowledge
    packs are copied from ``vault/templates/knowledge/{type}/`` into the
    profile's ``memory/`` folder (profiles spec §4, roadmap 4.3.4).

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
    data_dir:
        Optional root data directory (e.g. ``~/.agent-queue``).  When
        provided and a new profile is created, starter knowledge packs
        are copied into the profile's memory folder.
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

            # Copy starter knowledge for newly created agent-type profiles
            # (profiles spec §4, roadmap 4.3.4).
            if (
                change.operation == "created"
                and result.action == "created"
                and data_dir
                and path_id
                and path_id != "supervisor"
                and not path_id.startswith("project:")
            ):
                from src.vault import copy_starter_knowledge

                starter = copy_starter_knowledge(data_dir, path_id)
                if starter["copied"]:
                    logger.info(
                        "Copied starter knowledge for new profile '%s' via watcher: %s",
                        path_id,
                        ", ".join(starter["copied"]),
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
    data_dir: str | None = None,
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
    data_dir:
        Optional root data directory.  When provided, starter knowledge
        packs are copied for newly created profiles (roadmap 4.3.4).

    Returns
    -------
    list[str]
        The handler IDs returned by the watcher (for unregistration).
    """

    async def _handler(changes: list[VaultChange]) -> None:
        await on_profile_changed(changes, db=db, event_bus=event_bus, data_dir=data_dir)

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


def _find_profile_files(vault_root: str) -> list[tuple[str, str]]:
    """Find all existing profile files in the vault directory tree.

    Walks the vault tree and returns files matching :data:`PROFILE_PATTERNS`.

    Parameters
    ----------
    vault_root:
        Absolute path to the vault root directory.

    Returns
    -------
    list[tuple[str, str]]
        List of ``(absolute_path, relative_path)`` tuples for each
        matching profile file.
    """
    results: list[tuple[str, str]] = []

    if not os.path.isdir(vault_root):
        return results

    for dirpath, dirnames, filenames in os.walk(vault_root):
        # Skip hidden directories (.obsidian, .git, etc.)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for fname in filenames:
            if fname.startswith("."):
                continue

            full_path = os.path.join(dirpath, fname)
            rel_path = os.path.relpath(full_path, vault_root).replace(os.sep, "/")

            for pattern in PROFILE_PATTERNS:
                if fnmatch.fnmatch(rel_path, pattern):
                    results.append((full_path, rel_path))
                    break  # Don't add twice if multiple patterns match

    return results


async def scan_and_sync_existing_profiles(
    vault_root: str,
    db: Any,
    *,
    event_bus: EventBus | None = None,
) -> list[ProfileSyncResult]:
    """Scan the vault for existing profile files and sync them all to the DB.

    This is the **startup scan** — called once during orchestrator
    initialization to ensure profile files that already exist in the
    vault are reflected in the database.  The
    :class:`~src.vault_watcher.VaultWatcher` only detects *changes*
    after its initial snapshot; this function fills the gap by reading
    and syncing all existing profile files at boot time.

    Each file is read, parsed via :func:`~src.profiles.parser.parse_profile`,
    and synced via :func:`sync_profile_to_db`.  Files that fail to parse
    or sync produce logged errors (and optional event-bus notifications)
    but do not prevent other files from being processed.

    Parameters
    ----------
    vault_root:
        Absolute path to the vault root directory.
    db:
        A database instance implementing the ``DatabaseBackend`` protocol.
    event_bus:
        Optional :class:`~src.event_bus.EventBus` for failure notifications.

    Returns
    -------
    list[ProfileSyncResult]
        One result per profile file found (both successes and failures).
    """
    profile_files = _find_profile_files(vault_root)
    if not profile_files:
        logger.debug("Startup profile scan: no profile files found in %s", vault_root)
        return []

    results: list[ProfileSyncResult] = []

    for abs_path, rel_path in profile_files:
        fallback_id = derive_profile_id(rel_path)

        try:
            text = Path(abs_path).read_text(encoding="utf-8")
        except OSError:
            logger.error("Startup profile scan: could not read %s", abs_path, exc_info=True)
            results.append(
                ProfileSyncResult(
                    success=False,
                    action="none",
                    errors=[f"Could not read file: {abs_path}"],
                )
            )
            continue

        parsed = parse_profile(text)
        result = await sync_profile_to_db(
            parsed,
            db,
            source_path=abs_path,
            fallback_id=fallback_id,
        )
        results.append(result)

        if result.success:
            logger.info(
                "Startup profile scan: %s profile id=%s from %s",
                result.action,
                result.profile_id,
                rel_path,
            )
        else:
            logger.error(
                "Startup profile scan: failed to sync %s: %s",
                rel_path,
                result.errors,
            )
            await _emit_sync_failed(event_bus, result, rel_path)

    synced = sum(1 for r in results if r.success)
    failed = len(results) - synced
    logger.info(
        "Startup profile scan complete: %d file(s) found, %d synced, %d failed",
        len(results),
        synced,
        failed,
    )
    return results
