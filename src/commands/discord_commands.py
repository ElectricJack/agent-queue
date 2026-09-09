"""Shared-channel Discord transport commands."""

from __future__ import annotations


class DiscordCommandsMixin:
    """Discord command methods mixed into CommandHandler."""

    # Channel / thread housekeeping
    # ------------------------------------------------------------------

    async def _resolve_discord_channel(self, args: dict):
        """Resolve an explicit ``channel_id`` to a channel, or an error.

        Returns ``(channel, None)`` or ``(None, {"error": ...})``.
        """
        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot:
            return None, {"error": "Discord bot not available (is the daemon running?)"}

        channel_id = args.get("channel_id")
        if not channel_id:
            return None, {"error": "channel_id is required"}

        try:
            channel = bot.get_channel(int(channel_id)) or await bot.fetch_channel(int(channel_id))
        except Exception as e:
            return None, {"error": f"could not resolve channel {channel_id}: {e}"}
        return channel, None

    async def _cmd_discord_purge_channel(self, args: dict) -> dict:
        """Delete messages from a Discord channel.

        Dry-run unless ``confirm`` is true: the default reports how many
        messages *would* go, because this is irreversible and a mistyped
        channel id is unrecoverable.

        Discord only bulk-deletes messages under 14 days old.  Older ones must
        be removed one at a time, which is heavily rate-limited, so they are
        counted and reported rather than silently skipped — a purge that says
        "done" while leaving hundreds of old messages is worse than one that
        says what it could not do.
        """
        import datetime as _dt

        channel, err = await self._resolve_discord_channel(args)
        if err:
            return err

        limit = int(args.get("limit") or 1000)
        confirm = bool(args.get("confirm"))
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=14)

        recent, old = [], 0
        try:
            async for msg in channel.history(limit=limit):
                if msg.created_at >= cutoff:
                    recent.append(msg)
                else:
                    old += 1
        except Exception as e:
            return {"error": f"could not read channel history: {e}"}

        if not confirm:
            return {
                "success": True,
                "dry_run": True,
                "channel": getattr(channel, "name", str(channel.id)),
                "deletable": len(recent),
                "too_old_to_bulk_delete": old,
                "note": "re-run with confirm=true to delete",
            }

        deleted = 0
        try:
            for i in range(0, len(recent), 100):
                chunk = recent[i : i + 100]
                await channel.delete_messages(chunk)
                deleted += len(chunk)
        except Exception as e:
            return {
                "error": f"purge failed after {deleted} message(s): {e}",
                "deleted": deleted,
            }
        return {
            "success": True,
            "channel": getattr(channel, "name", str(channel.id)),
            "deleted": deleted,
            "too_old_to_bulk_delete": old,
        }
