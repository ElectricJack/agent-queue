"""Tests for the in-process Discord gate view (Wave 4)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import discord
import pytest


class _StubHandler:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.returns: dict = {"success": True, "gate_id": "g1", "unblocked_task_ids": []}

    async def execute(self, cmd: str, args: dict) -> dict:
        self.calls.append((cmd, dict(args)))
        return self.returns


@pytest.mark.asyncio
class TestGateView:
    async def test_approve_button_calls_gate_resolve_with_discord_user(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        view = GateView("g1", handler=handler)

        interaction = MagicMock()
        interaction.user = MagicMock(id=42)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        approve_btn = next(c for c in view.children if getattr(c, "label", None) == "Approve")
        await approve_btn.callback(interaction)

        assert handler.calls == [
            (
                "gate_resolve",
                {
                    "gate_id": "g1",
                    "resolved_by": "discord:42",
                    "resolution": "approve",
                },
            )
        ]
        interaction.followup.send.assert_awaited_once()
        assert all(getattr(c, "disabled", False) for c in view.children)

    async def test_deny_button_uses_deny_resolution(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        view = GateView("g1", handler=handler)
        interaction = MagicMock()
        interaction.user = MagicMock(id=99)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        deny_btn = next(c for c in view.children if getattr(c, "label", None) == "Deny")
        await deny_btn.callback(interaction)

        assert handler.calls[0][0] == "gate_resolve"
        assert handler.calls[0][1]["resolution"] == "deny"
        assert handler.calls[0][1]["resolved_by"] == "discord:99"

    async def test_error_response_shows_ephemeral_error(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        handler.returns = {"success": False, "error": "gate 'g1' not found"}
        view = GateView("g1", handler=handler)
        interaction = MagicMock()
        interaction.user = MagicMock(id=42)
        interaction.response = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        approve_btn = next(c for c in view.children if getattr(c, "label", None) == "Approve")
        await approve_btn.callback(interaction)

        interaction.followup.send.assert_awaited_once()
        kwargs = interaction.followup.send.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        args = interaction.followup.send.await_args.args
        msg_txt = args[0] if args else kwargs.get("content", "")
        assert "not found" in msg_txt
        assert not all(getattr(c, "disabled", False) for c in view.children)

    async def test_missing_handler_replies_ephemeral(self):
        from src.discord.gate_view import GateView

        view = GateView("g1", handler=None)
        interaction = MagicMock()
        interaction.user = MagicMock(id=1)
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        approve_btn = next(c for c in view.children if getattr(c, "label", None) == "Approve")
        await approve_btn.callback(interaction)
        interaction.response.send_message.assert_awaited_once()

    async def test_unauthorized_click_blocked_and_sends_ephemeral(self):
        """Unauthorized users cannot resolve gates via button clicks."""
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        bot = MagicMock()
        bot._is_authorized = MagicMock(return_value=False)
        view = GateView("g1", handler=handler, bot=bot)

        interaction = MagicMock()
        interaction.user = MagicMock(id=999)
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup = MagicMock()
        interaction.followup.send = AsyncMock()
        interaction.message = MagicMock()
        interaction.message.edit = AsyncMock()

        # interaction_check should reject
        allowed = await view.interaction_check(interaction)
        assert allowed is False
        interaction.response.send_message.assert_awaited_once()
        args, kwargs = interaction.response.send_message.await_args
        text = args[0] if args else kwargs.get("content", "")
        assert "not authorized" in text.lower()
        assert kwargs.get("ephemeral") is True
        # Handler must NOT have been called
        assert handler.calls == []

    async def test_authorized_click_passes_interaction_check(self):
        from src.discord.gate_view import GateView

        handler = _StubHandler()
        bot = MagicMock()
        bot._is_authorized = MagicMock(return_value=True)
        view = GateView("g1", handler=handler, bot=bot)

        interaction = MagicMock()
        interaction.user = MagicMock(id=42)
        interaction.response = MagicMock()
        interaction.response.send_message = AsyncMock()

        allowed = await view.interaction_check(interaction)
        assert allowed is True
        interaction.response.send_message.assert_not_awaited()

    async def test_on_timeout_evicts_gate_message(self):
        """View timeout must evict the tracked gate message to bound memory."""
        from src.discord.gate_view import GateView

        gate_messages: dict = {"g1": MagicMock()}

        def evict(gid: str) -> None:
            gate_messages.pop(gid, None)

        view = GateView("g1", handler=_StubHandler(), on_timeout_evict=evict)
        await view.on_timeout()
        assert "g1" not in gate_messages
        # Double-eviction safe
        await view.on_timeout()


@pytest.mark.asyncio
class TestGateEventHandlers:
    def _make_bot(self):
        bot = MagicMock()
        bot._send_message = AsyncMock(return_value=MagicMock(spec=discord.Message))
        bot._safe_api_call = AsyncMock(return_value=None)
        bot.agent = MagicMock()
        bot.agent.handler = _StubHandler()
        bot.orchestrator = MagicMock()
        return bot

    async def test_gate_created_posts_embed_with_view(self):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.created",
                {
                    "gate_id": "g1",
                    "gate_type": "approval",
                    "project_id": "p1",
                    "title": "Deploy to prod?",
                    "question": "Ship v1.2?",
                    "await_id": None,
                    "timeout_at": None,
                    "waiter_task_ids": ["t1", "t2"],
                },
            )

            assert bot._send_message.await_count == 1
            call = bot._send_message.await_args
            embed = call.kwargs.get("embed")
            view = call.kwargs.get("view")
            assert embed is not None
            assert "Deploy to prod?" in (embed.title or "")
            assert view is not None
            labels = [getattr(c, "label", None) for c in view.children]
            assert "Approve" in labels and "Deny" in labels
            assert "g1" in handler._gate_messages
        finally:
            handler.shutdown()

    async def test_gate_resolved_edits_message_and_removes_buttons(self):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        posted = MagicMock(spec=discord.Message)
        posted.edit = AsyncMock()
        bot._send_message = AsyncMock(return_value=posted)
        h = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.created",
                {
                    "gate_id": "g2",
                    "gate_type": "approval",
                    "project_id": "p1",
                    "title": "OK?",
                },
            )
            await bus.emit(
                "gate.resolved",
                {
                    "gate_id": "g2",
                    "project_id": "p1",
                    "resolved_by": "discord:42",
                    "resolution": "approve",
                    "unblocked_task_ids": ["t1"],
                },
            )
        finally:
            h.shutdown()

        assert bot._safe_api_call.await_count >= 1
        assert "g2" not in h._gate_messages

    async def test_gate_created_logs_warning_when_send_drops(self, caplog):
        """When _send_message returns None (HALT/rate-guard drop), log a warning."""
        import logging

        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        bot._send_message = AsyncMock(return_value=None)  # simulate HALT drop
        handler = DiscordNotificationHandler(bot, bus)
        try:
            with caplog.at_level(logging.WARNING, logger="src.discord.notification_handler"):
                await bus.emit(
                    "gate.created",
                    {
                        "gate_id": "gdrop",
                        "gate_type": "approval",
                        "project_id": "p1",
                        "title": "T",
                    },
                )
            assert any(
                "gdrop" in rec.message and rec.levelno >= logging.WARNING
                for rec in caplog.records
            ), f"expected warning mentioning gate id; got: {[r.message for r in caplog.records]}"
            assert "gdrop" not in handler._gate_messages
        finally:
            handler.shutdown()

    async def test_gate_view_timeout_evicts_from_handler_dict(self):
        """The GateView wired by the handler evicts _gate_messages on timeout."""
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.created",
                {
                    "gate_id": "gto",
                    "gate_type": "approval",
                    "project_id": "p1",
                    "title": "T",
                },
            )
            assert "gto" in handler._gate_messages
            # Grab the view attached and invoke its on_timeout
            view = bot._send_message.await_args.kwargs.get("view")
            assert view is not None
            await view.on_timeout()
            assert "gto" not in handler._gate_messages
        finally:
            handler.shutdown()

    async def test_gate_resolved_without_prior_created_is_noop(self):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        h = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.resolved",
                {
                    "gate_id": "unknown",
                    "project_id": "p1",
                    "resolved_by": "system",
                },
            )
        finally:
            h.shutdown()
        assert bot._safe_api_call.await_count == 0
