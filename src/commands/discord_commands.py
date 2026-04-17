"""Discord commands mixin — send_message."""

from __future__ import annotations


class DiscordCommandsMixin:
    """Discord command methods mixed into CommandHandler."""

    async def _cmd_send_message(self, args: dict) -> dict:
        """Post a message to a Discord channel."""
        channel_id = args.get("channel_id")
        content = args.get("content")
        if not channel_id or not content:
            return {"error": "channel_id and content are required"}
        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot:
            return {"error": "Discord bot not available"}
        try:
            channel = bot.get_channel(int(channel_id))
            if not channel:
                channel = await bot.fetch_channel(int(channel_id))
            await channel.send(content)
            return {"success": True, "channel_id": channel_id}
        except Exception as e:
            return {"error": f"Failed to send message: {e}"}
