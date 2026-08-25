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
        bot._is_authorized = MagicMock(return_value=True)
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
                "gdrop" in rec.message and rec.levelno >= logging.WARNING for rec in caplog.records
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

    async def test_gate_view_unauthorized_user_blocked_in_handler_integration(self):
        """Unauthorized users cannot resolve gates via button clicks in live flow."""
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        bus = EventBus(env="dev", validate_events=False)
        bot = self._make_bot()
        bot._is_authorized = MagicMock(return_value=False)
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "gate.created",
                {
                    "gate_id": "gauth",
                    "gate_type": "approval",
                    "project_id": "p1",
                    "title": "Test",
                },
            )
            assert "gauth" in handler._gate_messages
            # Extract the view that was wired
            view = bot._send_message.await_args.kwargs.get("view")
            assert view is not None

            # Create a mock interaction with an unauthorized user
            interaction = MagicMock()
            interaction.user = MagicMock(id=999)
            interaction.response = MagicMock()
            interaction.response.send_message = AsyncMock()

            # Call interaction_check — should reject and send ephemeral
            allowed = await view.interaction_check(interaction)
            assert allowed is False
            interaction.response.send_message.assert_awaited_once()
            args, kwargs = interaction.response.send_message.await_args
            text = args[0] if args else kwargs.get("content", "")
            assert "not authorized" in text.lower()
            assert kwargs.get("ephemeral") is True
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


@pytest.mark.asyncio
class TestGateCreatedPosting:
    """Only gates a person can resolve get an Approve/Deny embed.

    ``gate_commands.resolve`` refuses the generic path for ``routing`` gates
    ("routing gates can only be resolved via task_route"), so posting one put
    buttons in Discord that could never work.  The default pipeline attaches a
    routing gate to *every* new task, so a burst of task creation produced one
    dead approval prompt per task.
    """

    def _handler(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        h = DiscordNotificationHandler.__new__(DiscordNotificationHandler)
        h.bot = MagicMock()
        h.bot._send_message = AsyncMock(return_value=MagicMock())
        h._gate_messages = {}
        h._get_handler = lambda: _StubHandler()
        return h

    async def test_human_gate_is_posted(self):
        h = self._handler()
        await h._on_gate_created(
            {"gate_id": "g1", "project_id": "p1", "gate_type": "human", "title": "Approve?"}
        )
        h.bot._send_message.assert_awaited_once()

    @pytest.mark.parametrize(
        "gate_type", ["routing", "task", "pr-merged", "ci-run", "timer", "event"]
    )
    async def test_machine_resolved_gates_are_not_posted(self, gate_type):
        h = self._handler()
        await h._on_gate_created(
            {"gate_id": "g1", "project_id": "p1", "gate_type": gate_type, "title": "x"}
        )
        h.bot._send_message.assert_not_awaited()

    async def test_unknown_gate_type_is_still_posted(self):
        """Fail open: a redundant prompt beats swallowing a real approval."""
        h = self._handler()
        await h._on_gate_created(
            {"gate_id": "g1", "project_id": "p1", "gate_type": "brand-new", "title": "x"}
        )
        h.bot._send_message.assert_awaited_once()


class TestToolNoiseStripping:
    """Discord gets the supervisor's words, not its mechanics.

    The transcript reader injects ``[tool_use: X]`` / ``[tool_result] …``
    markers so the dashboard's live pane can show tool activity. Relaying
    those verbatim to Discord buried the agent's actual replies under one
    marker pair per tool call — a `pong` answer arrived under a dozen lines
    of Bash frames and a paste of `aq prime --help`.
    """

    def _strip(self, text):
        from src.discord.notification_handler import DiscordNotificationHandler

        return DiscordNotificationHandler._strip_tool_noise(text)

    def test_prose_survives(self):
        assert self._strip("pong") == "pong"
        assert self._strip("Here is the plan.\n\n1. Do X") == "Here is the plan.\n\n1. Do X"

    def test_markers_are_removed_but_prose_kept(self):
        assert self._strip("Checking.[tool_use: Bash][tool_result] done") == "Checking."

    def test_tool_only_messages_collapse_to_empty(self):
        assert self._strip("[tool_use: Bash]") == ""
        assert self._strip("[tool_result] Exit code 1\nError: boom") == ""
        assert self._strip("[tool_result] out[tool_use: Read]") == ""

    def test_bracketed_tool_output_does_not_leak(self):
        """Regression: a run ending at the next '[' left fragments behind.

        Tool stdout routinely contains brackets (usage strings, log levels,
        JSON), so the result run must extend to the next *marker*, not the
        next bracket.
        """
        raw = "[tool_use: Bash][tool_result] Usage: aq prime [OPTIONS]\n --task-id TEXT"
        assert self._strip(raw) == ""


@pytest.mark.asyncio
class TestAgentOutputRouting:
    """Agent narration goes to its task's thread — or nowhere.

    Three separate leaks put a working agent's running commentary into the
    project channel, where it reads as the supervisor talking:

    1. Non-``assistant`` transcript frames (the bootstrap prompt, injected
       inbox messages, harness ``system`` frames) were relayed verbatim.
    2. Entries whose text flattened to empty became literal ``[assistant]``
       lines via an ``entry.text or f"[{entry.type}]"`` fallback.
    3. With no thread registered — which is every task alive across a daemon
       restart, since ``_task_threads`` is in-memory — output fell back to the
       project channel.
    """

    def _handler(self):
        from src.discord.notification_handler import DiscordNotificationHandler

        h = DiscordNotificationHandler.__new__(DiscordNotificationHandler)
        h.bot = MagicMock()
        h.bot._send_message = AsyncMock(return_value=MagicMock())
        h._task_threads = {}
        return h

    def _event(self, **kw):
        base = {
            "event_type": "notify.task_message",
            "task_id": "t-1",
            "project_id": "p-1",
            "message": "hello",
            "message_type": "agent_output",
        }
        base.update(kw)
        return base

    async def test_user_frames_are_not_relayed(self):
        h = self._handler()
        await h._on_task_message(self._event(role="user", message="You are running task t-1..."))
        h.bot._send_message.assert_not_awaited()

    async def test_system_frames_are_not_relayed(self):
        h = self._handler()
        await h._on_task_message(self._event(role="system", message="<system-reminder>"))
        h.bot._send_message.assert_not_awaited()

    async def test_output_is_recovered_into_a_restored_thread(self):
        """After a restart the callback map is empty but the thread survives.

        Its id is persisted on ``tasks.discord_thread_id``, so the callbacks
        are rebuilt on demand and the output lands in the thread — visible,
        and still not in the project channel.
        """
        h = self._handler()
        sent = []

        async def send_thread(text):
            sent.append(text)

        h.bot.thread_callbacks_for_task = AsyncMock(return_value=(send_thread, None))

        await h._on_task_message(self._event(role="assistant", message="Now I'll implement."))

        assert sent == ["Now I'll implement."]
        h.bot._send_message.assert_not_awaited()
        assert "t-1" in h._task_threads, "rebuilt callbacks should be cached"

    async def test_rebuild_is_cached_not_repeated_per_message(self):
        h = self._handler()

        async def send_thread(text):
            return None

        h.bot.thread_callbacks_for_task = AsyncMock(return_value=(send_thread, None))
        for _ in range(3):
            await h._on_task_message(self._event(role="assistant", message="x"))
        assert h.bot.thread_callbacks_for_task.await_count == 1

    async def test_output_is_dropped_when_no_thread_can_be_recovered(self):
        """Never fall back to the project channel — that reads as the
        supervisor talking when it is a task agent narrating."""
        h = self._handler()
        h.bot.thread_callbacks_for_task = AsyncMock(return_value=None)

        await h._on_task_message(self._event(role="assistant", message="Now I'll implement."))
        h.bot._send_message.assert_not_awaited()

    async def test_assistant_output_reaches_its_thread(self):
        h = self._handler()
        sent = []

        async def send_thread(text):
            sent.append(text)

        h._task_threads["t-1"] = (send_thread, None)
        await h._on_task_message(self._event(role="assistant", message="Now I'll implement."))
        assert sent == ["Now I'll implement."]

    async def test_brief_notifications_still_reach_the_channel(self):
        """Curated notifications are the channel's purpose — never filtered."""
        h = self._handler()
        await h._on_task_message(self._event(message_type="brief", message="Task completed"))
        h.bot._send_message.assert_awaited_once()
