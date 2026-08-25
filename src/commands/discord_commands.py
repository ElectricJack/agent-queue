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

    # ------------------------------------------------------------------
    # Channel / thread housekeeping
    # ------------------------------------------------------------------

    async def _resolve_discord_channel(self, args: dict):
        """Resolve ``channel_id`` or ``project_id`` to a channel, or an error.

        Returns ``(channel, None)`` or ``(None, {"error": ...})``.
        """
        bot = getattr(self.orchestrator, "_discord_bot", None)
        if not bot:
            return None, {"error": "Discord bot not available (is the daemon running?)"}

        channel_id = args.get("channel_id")
        project_id = args.get("project_id")
        if not channel_id and project_id:
            project = await self.db.get_project(str(project_id))
            channel_id = getattr(project, "discord_channel_id", None) if project else None
            if not channel_id:
                return None, {"error": f"project '{project_id}' has no Discord channel"}
        if not channel_id:
            return None, {"error": "channel_id or project_id is required"}

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

    async def _cmd_discord_cleanup_threads(self, args: dict) -> dict:
        """Archive or delete threads in a Discord channel.

        Defaults are the conservative ones: ``mode="archive"`` (reversible,
        keeps the history) over ``delete``, and ``only_closed=True`` so a
        thread whose task is still running is left alone.  ``only_closed``
        matches threads back to tasks through ``tasks.discord_thread_id``;
        a thread with no matching task row counts as closed, since nothing
        live refers to it.

        Dry-run unless ``confirm`` is true.
        """
        channel, err = await self._resolve_discord_channel(args)
        if err:
            return err

        mode = str(args.get("mode") or "archive").lower()
        if mode not in ("archive", "delete"):
            return {"error": "mode must be 'archive' or 'delete'"}
        only_closed = args.get("only_closed")
        only_closed = True if only_closed is None else bool(only_closed)
        confirm = bool(args.get("confirm"))
        limit = int(args.get("limit") or 500)

        threads = list(getattr(channel, "threads", []) or [])
        try:
            async for t in channel.archived_threads(limit=limit):
                threads.append(t)
        except Exception:
            # Archived listing is best-effort: a missing permission should not
            # stop us cleaning the active ones.
            pass

        live_thread_ids: set[str] = set()
        if only_closed:
            try:
                rows = await self.db.list_tasks()
                live_thread_ids = {
                    str(t.discord_thread_id)
                    for t in rows
                    if getattr(t, "discord_thread_id", None)
                    and getattr(t.status, "value", t.status)
                    not in ("COMPLETED", "FAILED", "CANCELLED")
                }
            except Exception:
                return {"error": "could not read tasks to determine which threads are live"}

        targets = [
            t for t in threads
            if not (only_closed and str(t.id) in live_thread_ids)
            and not (mode == "archive" and getattr(t, "archived", False))
        ]

        if not confirm:
            return {
                "success": True,
                "dry_run": True,
                "channel": getattr(channel, "name", str(channel.id)),
                "threads_found": len(threads),
                "would_" + mode: len(targets),
                "skipped_live": len(threads) - len(targets),
                "note": "re-run with confirm=true to apply",
            }

        done, failed = 0, 0
        for t in targets:
            try:
                if mode == "delete":
                    await t.delete()
                else:
                    await t.edit(archived=True)
                done += 1
            except Exception:
                failed += 1
        return {
            "success": True,
            "channel": getattr(channel, "name", str(channel.id)),
            "mode": mode,
            mode + "d": done,
            "failed": failed,
            "skipped_live": len(threads) - len(targets),
        }
