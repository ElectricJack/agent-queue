"""Discord commands mixin — send_message."""

from __future__ import annotations

import discord


class DiscordCommandsMixin:
    """Discord command methods mixed into CommandHandler."""

    async def _cmd_send_message(self, args: dict) -> dict:
        """Post a message to a Discord channel.

        Accepts ``content`` (text), ``embeds`` (list of embed dicts in Discord's
        JSON format), or both. At least one must be provided.
        """
        channel_id = args.get("channel_id")
        content = args.get("content")
        embeds_raw = args.get("embeds") or []
        if not channel_id:
            return {"error": "channel_id is required"}
        if not content and not embeds_raw:
            return {"error": "content or embeds is required"}

        try:
            embeds = [discord.Embed.from_dict(e) for e in embeds_raw]
        except (TypeError, ValueError) as e:
            return {"error": f"Invalid embed payload: {e}"}

        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot:
            return {"error": "Discord bot not available"}
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))
            send_kwargs: dict = {}
            if content:
                send_kwargs["content"] = content
            if embeds:
                send_kwargs["embeds"] = embeds
            await channel.send(**send_kwargs)
            return {"success": True, "channel_id": channel_id}
        except Exception as e:
            return {"error": f"Failed to send message: {e}"}
