"""Response models for Discord channel and thread housekeeping."""

from __future__ import annotations

from pydantic import BaseModel


class DiscordPurgeChannelResponse(BaseModel):
    """Result of ``discord_purge_channel``.

    Both shapes share one model because the command is a dry run by default:
    without ``confirm`` it reports ``deletable`` and sets ``dry_run``, and with
    it reports ``deleted``.  ``too_old_to_bulk_delete`` is always present —
    Discord refuses to bulk-delete messages over 14 days old, and a purge that
    reported success while silently leaving hundreds behind would be worse than
    one that says what it could not reach.
    """

    success: bool = True
    channel: str
    dry_run: bool = False
    deletable: int | None = None
    deleted: int | None = None
    too_old_to_bulk_delete: int = 0
    note: str | None = None


class DiscordCleanupThreadsResponse(BaseModel):
    """Result of ``discord_cleanup_threads``.

    ``skipped_live`` is reported rather than inferred: the useful question
    after a cleanup is "what did it leave alone, and why", and the answer is
    threads whose task is still running.
    """

    success: bool = True
    channel: str
    dry_run: bool = False
    mode: str | None = None
    threads_found: int | None = None
    would_archive: int | None = None
    would_delete: int | None = None
    archived: int | None = None
    deleted: int | None = None
    failed: int | None = None
    skipped_live: int = 0
    note: str | None = None
    #: Set when archived threads could not be listed — the counts above then
    #: describe only the active ones.
    warning: str | None = None


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "discord_purge_channel": DiscordPurgeChannelResponse,
    "discord_cleanup_threads": DiscordCleanupThreadsResponse,
}
