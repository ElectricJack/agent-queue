"""Discord commands mixin — send_message, get_system_channel."""

from __future__ import annotations

import discord


class DiscordCommandsMixin:
    """Discord command methods mixed into CommandHandler."""

    # Aliases for the merged single-broadcast-channel config model.
    # Older configs used separate "control" and "notifications" keys; config.py
    # collapses them into a single "channel" entry, so callers requesting any
    # of these legacy names should resolve to the same channel.
    _SYSTEM_CHANNEL_ALIASES = {
        "notifications": "channel",
        "control": "channel",
    }

    async def _cmd_get_system_channel(self, args: dict) -> dict:
        """Resolve a system-level Discord channel by its config key.

        The config key (e.g. ``channel``, ``notifications``, ``agent_questions``)
        is mapped to a Discord channel name via ``config.discord.channels`` and
        looked up in the guild. Returns the channel id so callers can pass it
        to ``send_message`` — there is no tool that sends by name.

        ``notifications`` and ``control`` are accepted as aliases for
        ``channel`` to match the merged single-broadcast-channel config model.
        """
        name = args.get("name")
        if not name:
            return {
                "error": "name is required (e.g. 'notifications', 'channel', 'agent_questions')"
            }

        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot or not getattr(bot, "_guild", None):
            return {"error": "Discord bot or guild not available"}

        channels_cfg = getattr(bot.config.discord, "channels", None) or {}
        resolved_key = self._SYSTEM_CHANNEL_ALIASES.get(name, name)
        channel_name = channels_cfg.get(resolved_key)
        if not channel_name:
            known = sorted(set(channels_cfg.keys()) | set(self._SYSTEM_CHANNEL_ALIASES.keys()))
            return {
                "error": (
                    f"No channel configured for '{name}'. Known keys (including aliases): {known}"
                )
            }

        for ch in bot._guild.text_channels:
            if ch.name == channel_name:
                return {
                    "name": name,
                    "channel_name": ch.name,
                    "channel_id": str(ch.id),
                }
        return {"error": f"Channel '#{channel_name}' not found in guild"}

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
