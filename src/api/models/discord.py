"""Response models for explicit Discord channel housekeeping."""

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


RESPONSE_MODELS: dict[str, type[BaseModel]] = {
    "discord_purge_channel": DiscordPurgeChannelResponse,
}
