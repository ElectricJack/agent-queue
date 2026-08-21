"""Tests for the P4 chat routing cutover and Supervisor.initialize() hardening.

Covers:
- ``Supervisor.initialize()`` returns False (never raises) when chat-provider
  construction fails — daemon boot must degrade gracefully.
- Discord ``on_message`` chat routing:
    * flags off (default): legacy ``self.agent.chat(...)`` is called.
    * flags on (``supervisor_agent.enabled`` and not ``legacy_chat``): the
      chat call is replaced by ``message.send`` with a supervisor session
      recipient and ``self.agent.chat`` is NOT called.
- ``invoke_llm`` plugin fallback: the legacy path still resolves to
  ``supervisor.chat`` (spec §9 row 3 — behaviour unchanged in this phase).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.commands.handler import CommandHandler
from src.config import MessagesConfig, SupervisorAgentConfig
from src.database import Database
from src.models import Project


# ---------------------------------------------------------------------------
# Supervisor.initialize() hardening
# ---------------------------------------------------------------------------


class TestInitializeHardening:
    """``initialize()`` must return False (not raise) on provider errors.

    Boot at ``src/main.py:105`` treats a False return as non-fatal and only
    logs a warning; a raised ``ValueError`` (e.g. google-genai when its API
    key env var is missing) tears the daemon down.  See supervisor-agent.md
    §9 row 1.
    """

    def test_returns_false_when_provider_raises(self, tmp_path):
        from src.config import AppConfig
        from src.runtimes.supervisor import Supervisor

        cfg = AppConfig()
        cfg.data_dir = str(tmp_path)  # no vault profile -> use base config
        orch = MagicMock()
        orch.llm_logger = None
        sup = Supervisor(orch, cfg, llm_logger=None)

        with patch(
            "src.runtimes.supervisor.create_chat_provider",
            side_effect=ValueError("GOOGLE_API_KEY not set"),
        ):
            # Must NOT raise — must return False.
            result = sup.initialize()

        assert result is False
        assert sup._provider is None

    def test_returns_false_when_provider_returns_none(self, tmp_path):
        from src.config import AppConfig
        from src.runtimes.supervisor import Supervisor

        cfg = AppConfig()
        cfg.data_dir = str(tmp_path)
        orch = MagicMock()
        orch.llm_logger = None
        sup = Supervisor(orch, cfg, llm_logger=None)

        with patch(
            "src.runtimes.supervisor.create_chat_provider",
            return_value=None,
        ):
            result = sup.initialize()

        assert result is False


# ---------------------------------------------------------------------------
# Chat routing cutover — the flag decision
# ---------------------------------------------------------------------------


def _make_handler_with_messages(db, enabled=True):
    """Build a CommandHandler wired to a real DB with messages enabled."""
    bus = MagicMock()
    bus.emit = AsyncMock()
    orch = MagicMock()
    orch.db = db
    orch.bus = bus
    orch.plugin_registry = None

    config = MagicMock()
    config.messages = MessagesConfig(enabled=enabled)
    config.supervisor_agent = SupervisorAgentConfig(enabled=True, legacy_chat=False)

    handler = CommandHandler(orch, config)
    handler._active_project_id = None
    return handler, bus


@pytest.fixture
async def db(tmp_path):
    """Real Database with one project."""
    d = Database(str(tmp_path / "cutover.db"))
    await d.initialize()
    await d.create_project(Project(id="p1", name="test"))
    yield d
    await d.close()


class TestRoutingDecision:
    """Verify the flag decision that ``on_message`` uses.

    The chat routing decision is a boolean derived from two flags; testing
    the decision helper directly is enough — the on_message wiring is a
    thin conditional over it (integration test lives below).
    """

    def test_legacy_when_flags_off(self):
        from src.discord.bot import supervisor_session_routing_enabled

        cfg = MagicMock()
        cfg.supervisor_agent = SupervisorAgentConfig(enabled=False, legacy_chat=True)
        assert supervisor_session_routing_enabled(cfg) is False

    def test_legacy_when_only_enabled(self):
        from src.discord.bot import supervisor_session_routing_enabled

        cfg = MagicMock()
        cfg.supervisor_agent = SupervisorAgentConfig(enabled=True, legacy_chat=True)
        # legacy_chat still true → keep legacy behaviour
        assert supervisor_session_routing_enabled(cfg) is False

    def test_new_path_when_enabled_and_not_legacy(self):
        from src.discord.bot import supervisor_session_routing_enabled

        cfg = MagicMock()
        cfg.supervisor_agent = SupervisorAgentConfig(enabled=True, legacy_chat=False)
        assert supervisor_session_routing_enabled(cfg) is True

    def test_missing_config_defaults_to_legacy(self):
        from src.discord.bot import supervisor_session_routing_enabled

        cfg = MagicMock(spec=[])  # no supervisor_agent attribute
        assert supervisor_session_routing_enabled(cfg) is False


class TestMessageSendPath:
    """When flags are on, on_message should invoke ``message.send`` with the
    correct supervisor session address.  Instead of driving the whole
    ``on_message`` handler (which is ~300 lines of Discord state) we assert
    that a call through ``handler.execute("message_send", ...)`` with the
    address the cutover uses actually queues a row for the supervisor
    session — the same call the cutover makes.
    """

    async def test_send_to_supervisor_session_queues_row(self, db):
        handler, bus = _make_handler_with_messages(db)
        result = await handler.execute(
            "message_send",
            {
                "project_id": "p1",
                "to_kind": "session",
                "to_id": "supervisor-p1",
                "from_kind": "user",
                "from_id": "discord:42",
                "body": "hello supervisor",
            },
        )
        assert "error" not in result, result
        assert result["state"] == "queued"

        stored = await db.get_message(result["message_id"])
        assert stored is not None
        assert stored.to_kind == "session"
        assert stored.to_id == "supervisor-p1"
        assert stored.from_kind == "user"

        # A message.sent event was emitted on the recording bus.
        emitted = [c.args for c in bus.emit.await_args_list]
        assert any(a[0] == "message.sent" for a in emitted)

    async def test_new_path_skips_thinking_view_and_reacts_on_success(self, db):
        """On the supervisor-session path, on_message must not post the
        legacy ThinkingView; it must acknowledge the enqueue with a 📬
        reaction on the user's message and not call self.agent.chat.
        """
        from src.discord.bot import AgentQueueBot

        handler, bus = _make_handler_with_messages(db)
        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(enabled=True, legacy_chat=False)
        bot.config.messages = MessagesConfig(enabled=True)
        bot.agent = MagicMock()
        bot.agent.handler = handler
        bot.agent.chat = AsyncMock()  # must NOT be called
        bot.agent.is_ready = True
        bot.agent.is_model_loaded = AsyncMock(return_value=True)
        bot.agent._active_project_id = None
        bot.agent.set_active_project = MagicMock()
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._thinking_msg_ids = set()
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])
        bot._delete_thinking_msg = AsyncMock()
        bot._safe_api_call = AsyncMock(return_value=None)

        async def _safe_api_call(coro, **_):
            try:
                return await coro
            except Exception:
                return None

        bot._safe_api_call = AsyncMock(side_effect=_safe_api_call)

        message = MagicMock()
        message.author = MagicMock()
        message.author.id = 42
        message.author.display_name = "alice"
        message.author.bot = False
        message.channel = MagicMock()
        message.channel.id = 123
        message.channel.typing = MagicMock()
        message.channel.typing.return_value.__aenter__ = AsyncMock()
        message.channel.typing.return_value.__aexit__ = AsyncMock()
        message.attachments = []
        message.content = "hi supervisor"
        message.reference = None
        message.id = 999
        message.created_at = MagicMock()
        message.created_at.timestamp = MagicMock(return_value=1.0)
        message.add_reaction = AsyncMock()
        message.reply = AsyncMock()

        _bot_user = MagicMock(id=1)
        type(bot).user = _bot_user
        message.mentions = [_bot_user]

        with patch.object(
            AgentQueueBot,
            "ThinkingView",
            side_effect=AssertionError("thinking view constructed"),
        ):
            await AgentQueueBot.on_message(bot, message)

        message.add_reaction.assert_awaited_once_with("\U0001f4ec")
        bot.agent.chat.assert_not_awaited()
        bot._delete_thinking_msg.assert_not_called()

    async def test_new_path_surfaces_message_send_error(self, db):
        """A message_send error result must produce a visible error reply
        and no 📬 reaction."""
        from src.discord.bot import AgentQueueBot

        handler, _ = _make_handler_with_messages(db)

        async def _fake_execute(cmd, args):
            if cmd == "message_send":
                return {"error": "queue full"}
            return {"success": True}

        handler.execute = _fake_execute  # type: ignore[assignment]

        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(enabled=True, legacy_chat=False)
        bot.config.messages = MessagesConfig(enabled=True)
        bot.agent = MagicMock()
        bot.agent.handler = handler
        bot.agent.chat = AsyncMock()
        bot.agent.is_ready = True
        bot.agent.is_model_loaded = AsyncMock(return_value=True)
        bot.agent._active_project_id = None
        bot.agent.set_active_project = MagicMock()
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._thinking_msg_ids = set()
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])
        bot._delete_thinking_msg = AsyncMock()
        bot._send_long_message = AsyncMock()

        message = MagicMock()
        message.author = MagicMock(id=42, display_name="alice", bot=False)
        message.channel = MagicMock(id=123)
        message.channel.typing = MagicMock()
        message.channel.typing.return_value.__aenter__ = AsyncMock()
        message.channel.typing.return_value.__aexit__ = AsyncMock()
        message.attachments = []
        message.content = "hi"
        message.reference = None
        message.id = 1000
        message.created_at = MagicMock()
        message.created_at.timestamp = MagicMock(return_value=1.0)
        message.add_reaction = AsyncMock()
        message.reply = AsyncMock()
        _bot_user = MagicMock(id=1)
        type(bot).user = _bot_user
        message.mentions = [_bot_user]

        await AgentQueueBot.on_message(bot, message)

        assert bot._send_long_message.await_count == 1
        posted = bot._send_long_message.await_args.args[1]
        assert "Message queue error" in posted
        assert "queue full" in posted
        message.add_reaction.assert_not_awaited()


# ---------------------------------------------------------------------------
# Notification-handler wiring: message.sent -> Discord render
# ---------------------------------------------------------------------------


class TestMessageSentRenderer:
    """The Discord notification handler must subscribe to ``message.sent``
    so replies from the supervisor session reach the originating project
    channel.  Renderer scope is intentionally strict (see review of
    commit f365e6c4): only ``from_kind=session`` replies whose
    ``thread_id`` carries the ``discord:`` prefix are posted; other
    surfaces' ``to_kind=user`` messages must not be double-rendered.
    """

    async def _make_bot(self, db, *, channel=None):
        bot = MagicMock()
        bot.orchestrator = MagicMock()
        bot.orchestrator.db = db
        bot._project_channels = {"p1": MagicMock()}
        bot._send_message = AsyncMock()
        bot._send_long_message = AsyncMock()
        bot.get_channel = MagicMock(return_value=channel)
        return bot

    async def test_subscribes_and_posts_body_to_project_channel(self, db):
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        reply = await db.create_message(
            project_id="p1",
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="discord:42",
            body="the answer is 42",
            thread_id="discord:999",
        )
        bus = EventBus(env="dev", validate_events=False)
        parsed_channel = MagicMock()
        bot = await self._make_bot(db, channel=parsed_channel)

        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "message.sent",
                {
                    "message_id": reply.id,
                    "project_id": "p1",
                    "from_kind": "session",
                    "from_id": "supervisor-p1",
                    "to_kind": "user",
                    "to_id": "discord:42",
                    "thread_id": "discord:999",
                },
            )
        finally:
            handler.shutdown()

        # get_channel was called with the parsed channel id, and the reply
        # was posted to THAT channel (not via the project fallback).
        bot.get_channel.assert_called_once_with(999)
        assert bot._send_long_message.await_count == 1
        args = bot._send_long_message.await_args.args
        assert args[0] is parsed_channel
        assert "the answer is 42" in args[1]
        assert bot._send_message.await_count == 0

    async def test_skips_non_discord_thread_id(self, db):
        """A ``message.sent`` from another surface (or with no thread_id)
        must NOT trigger a Discord post."""
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        reply = await db.create_message(
            project_id="p1",
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="slack:U42",
            body="not for discord",
            thread_id="slack:C1/T1",
        )
        bus = EventBus(env="dev", validate_events=False)
        bot = await self._make_bot(db)
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "message.sent",
                {
                    "message_id": reply.id,
                    "project_id": "p1",
                    "from_kind": "session",
                    "from_id": "supervisor-p1",
                    "to_kind": "user",
                    "to_id": "slack:U42",
                    "thread_id": "slack:C1/T1",
                },
            )
            # Also assert absent thread_id is skipped.
            reply2 = await db.create_message(
                project_id="p1",
                from_kind="session",
                from_id="supervisor-p1",
                to_kind="user",
                to_id="discord:42",
                body="no thread",
            )
            await bus.emit(
                "message.sent",
                {
                    "message_id": reply2.id,
                    "project_id": "p1",
                    "from_kind": "session",
                    "from_id": "supervisor-p1",
                    "to_kind": "user",
                    "to_id": "discord:42",
                },
            )
        finally:
            handler.shutdown()

        assert bot._send_message.await_count == 0
        assert bot._send_long_message.await_count == 0

    async def test_skips_user_authored_echo(self, db):
        """A ``message.sent`` with ``from_kind=user`` (echoed user message)
        must NOT trigger a Discord post — only ``from_kind=session``
        replies are rendered."""
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        reply = await db.create_message(
            project_id="p1",
            from_kind="user",
            from_id="discord:42",
            to_kind="user",
            to_id="discord:43",
            body="user echo",
            thread_id="discord:999",
        )
        bus = EventBus(env="dev", validate_events=False)
        bot = await self._make_bot(db, channel=MagicMock())
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit(
                "message.sent",
                {
                    "message_id": reply.id,
                    "project_id": "p1",
                    "from_kind": "user",
                    "from_id": "discord:42",
                    "to_kind": "user",
                    "to_id": "discord:43",
                    "thread_id": "discord:999",
                },
            )
        finally:
            handler.shutdown()

        assert bot._send_message.await_count == 0
        assert bot._send_long_message.await_count == 0

    async def test_end_to_end_delivery_emits_payload_that_renders(self, db):
        """Behavioural integration: run the real ``MessageDeliveryEngine``
        with a bus spy, assert the emitted ``message.sent`` payload carries
        the fields the renderer relies on (message_id, project_id, to_kind,
        from_kind, thread_id), then feed that exact payload back into
        ``_on_message_sent`` and assert a Discord post happens.  Closes
        the review's "payload shape unverified" gap.
        """
        from dataclasses import dataclass, field

        from src.config import MessagesConfig
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus
        from src.messages.delivery import MessageDeliveryEngine

        @dataclass
        class _RecBus:
            events: list = field(default_factory=list)

            async def emit(self, event, payload):
                self.events.append((event, dict(payload)))

        class _Sessions:
            async def activity(self, **_):
                return "idle"

            async def ensure_started(self, **_):
                return True

            async def nudge(self, **_):
                return True

            async def tail_assistant_turn(self, **_):
                return None

        # Queue a supervisor→user reply with a discord thread_id.
        reply = await db.create_message(
            project_id="p1",
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="discord:42",
            body="delivered body",
            thread_id="discord:1234",
        )

        spy = _RecBus()
        engine = MessageDeliveryEngine(
            db=db,
            sessions=_Sessions(),
            config=MessagesConfig(enabled=True),
            bus=spy,
        )
        result = await engine.run_delivery_pass()
        assert result["delivered"] == 1

        sent = [(e, p) for e, p in spy.events if e == "message.sent"]
        assert len(sent) == 1
        payload = sent[0][1]
        # Payload shape assertion — the exact fields the renderer needs.
        for key in ("message_id", "project_id", "to_kind", "from_kind", "thread_id"):
            assert key in payload, f"payload missing {key}: {payload}"
        assert payload["message_id"] == reply.id
        assert payload["project_id"] == "p1"
        assert payload["to_kind"] == "user"
        assert payload["from_kind"] == "session"
        assert payload["thread_id"] == "discord:1234"

        # Feed that exact payload into the renderer.
        bus = EventBus(env="dev", validate_events=False)
        parsed_channel = MagicMock()
        bot = await self._make_bot(db, channel=parsed_channel)
        handler = DiscordNotificationHandler(bot, bus)
        try:
            await bus.emit("message.sent", payload)
        finally:
            handler.shutdown()

        assert bot._send_long_message.await_count == 1
        args = bot._send_long_message.await_args.args
        assert args[0] is parsed_channel
        assert "delivered body" in args[1]


# ---------------------------------------------------------------------------
# Plugin invoke_llm fallback (spec §9 row 3 — unchanged)
# ---------------------------------------------------------------------------


class TestInvokeLLMFallback:
    """The plugin ``invoke_llm`` fallback still delegates to
    ``supervisor.chat`` when no per-call model/provider override is given.
    Behaviour is unchanged in this task; the test guards against accidental
    regression from the cutover.
    """

    async def test_default_path_calls_supervisor_chat(self):
        # Verify the plugin registry gets a callback and that the callback
        # source-code still calls ``supervisor.chat``.  Static-import check
        # keeps this fast and avoids standing up the full orchestrator.
        import inspect

        from src.orchestrator import core

        src = inspect.getsource(core.Orchestrator.set_supervisor)
        # Guard: the fallback still routes through supervisor.chat when no
        # per-call override is supplied.
        assert "supervisor.chat(" in src
