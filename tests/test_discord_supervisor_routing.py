"""Discord chat routing to the supervisor *session* (post-cutover).

Covers:
- Discord ``on_message`` chat routing:
    * ``supervisor_agent.enabled=True`` in a bound project channel:
      ``message.send`` is invoked with a supervisor session recipient.
    * enabled but unbound channel: user gets an "isn't bound to a project"
      hint and no ``message.send`` fires.
    * disabled: user gets a "chat is disabled" hint.
- ``SupervisorAgentConfig`` no longer carries ``legacy_chat``; old YAML that
  includes the key must load without error.
- Bot module carries no in-process chat agent state: no ``self.agent``, and
  the bot never calls a ``.chat()`` brain.
- The ``message.sent`` renderer that posts a session's reply back to the
  project channel.

Split out of the former ``test_supervisor_cutover.py`` when the in-process
Supervisor was deleted; the ``Supervisor.initialize()`` hardening tests went
with the class.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import MessagesConfig, SupervisorAgentConfig
from src.database import Database
from src.models import Project


# ---------------------------------------------------------------------------
# legacy_chat field removal
# ---------------------------------------------------------------------------


def test_supervisor_agent_config_has_no_legacy_chat():
    from src.config import SupervisorAgentConfig

    cfg = SupervisorAgentConfig()
    assert not hasattr(cfg, "legacy_chat")


def test_config_loader_ignores_legacy_chat_key(tmp_path):
    """A YAML file still carrying legacy_chat must load without error."""
    import yaml

    from src.config import load_config

    cfg_path = tmp_path / "config.yaml"
    # Include the minimum required fields so load_config doesn't raise on
    # validation.  Use enabled=False to avoid the messages/sessions prereq.
    cfg_path.write_text(
        yaml.dump(
            {
                "discord": {"bot_token": "test-token", "guild_id": "123"},
                "database_path": str(tmp_path / "test.db"),
                "supervisor_agent": {"enabled": False, "legacy_chat": False},
            }
        )
    )
    cfg = load_config(str(cfg_path))
    assert cfg.supervisor_agent.enabled is False


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
    config.supervisor_agent = SupervisorAgentConfig(enabled=True)

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
        cfg.supervisor_agent = SupervisorAgentConfig(enabled=False)
        assert supervisor_session_routing_enabled(cfg) is False

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
        legacy ThinkingView (which has been deleted); it must acknowledge
        the enqueue with a 📬 reaction on the user's message and NEVER
        touch a chat brain.
        """
        from src.discord.bot import AgentQueueBot

        handler, bus = _make_handler_with_messages(db)
        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(enabled=True)
        bot.config.messages = MessagesConfig(enabled=True)
        bot.orchestrator = MagicMock()
        bot.orchestrator._command_handler = handler
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])

        async def _safe_api_call(coro, **_):
            try:
                return await coro
            except Exception:
                return None

        bot._safe_api_call = AsyncMock(side_effect=_safe_api_call)
        bot._send_long_message = AsyncMock()

        message = MagicMock()
        message.author = MagicMock()
        message.author.id = 42
        message.author.display_name = "alice"
        message.author.bot = False
        message.channel = MagicMock()
        message.channel.id = 123
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

        await AgentQueueBot.on_message(bot, message)

        message.add_reaction.assert_awaited_once_with("\U0001f4ec")
        bot._send_long_message.assert_not_called()

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
        bot.config.supervisor_agent = SupervisorAgentConfig(enabled=True)
        bot.config.messages = MessagesConfig(enabled=True)
        bot.orchestrator = MagicMock()
        bot.orchestrator._command_handler = handler
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])
        bot._send_long_message = AsyncMock()
        bot._safe_api_call = AsyncMock(return_value=None)

        message = MagicMock()
        message.author = MagicMock(id=42, display_name="alice", bot=False)
        message.channel = MagicMock(id=123)
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

    async def test_unbound_channel_hints_and_skips_send(self, db):
        """A message in an unbound channel (project_channel_id None) with
        routing enabled must reply with an 'isn't bound to a project' hint
        and NOT invoke message_send.
        """
        from src.discord.bot import AgentQueueBot

        handler, _ = _make_handler_with_messages(db)
        called: list[tuple[str, dict]] = []

        async def _fake_execute(cmd, args):
            called.append((cmd, args))
            return {"success": True}

        handler.execute = _fake_execute  # type: ignore[assignment]

        # Simulate a global bot channel — not bound to a project.
        global_channel = MagicMock(id=555)
        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(enabled=True)
        bot.config.messages = MessagesConfig(enabled=True)
        bot.orchestrator = MagicMock()
        bot.orchestrator._command_handler = handler
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._channel = global_channel
        bot._channel_to_project = {}
        bot._project_channels = {}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])
        bot._send_long_message = AsyncMock()
        bot._safe_api_call = AsyncMock(return_value=None)

        message = MagicMock()
        message.author = MagicMock(id=42, display_name="alice", bot=False)
        message.channel = global_channel
        message.attachments = []
        message.content = "hello"
        message.reference = None
        message.id = 2001
        message.created_at = MagicMock()
        message.created_at.timestamp = MagicMock(return_value=1.0)
        message.add_reaction = AsyncMock()
        message.reply = AsyncMock()
        _bot_user = MagicMock(id=1)
        type(bot).user = _bot_user
        message.mentions = []  # not mentioned; hits the bot channel path

        await AgentQueueBot.on_message(bot, message)

        # message_send must NOT have been invoked.
        assert not any(cmd == "message_send" for cmd, _ in called)
        # A reply hint was posted mentioning the unbound state.
        message.reply.assert_awaited()
        posted = message.reply.await_args.args[0]
        assert "isn't bound to a project" in posted

    async def test_routing_disabled_replies_hint(self, db):
        """With supervisor_agent.enabled=False, a message in a bound channel
        gets a "chat is disabled" hint and no message_send is invoked.
        """
        from src.discord.bot import AgentQueueBot

        handler, _ = _make_handler_with_messages(db, enabled=False)
        called: list[tuple[str, dict]] = []

        async def _fake_execute(cmd, args):
            called.append((cmd, args))
            return {"success": True}

        handler.execute = _fake_execute  # type: ignore[assignment]

        bot = AgentQueueBot.__new__(AgentQueueBot)
        bot.config = MagicMock()
        bot.config.supervisor_agent = SupervisorAgentConfig(enabled=False)
        bot.config.messages = MessagesConfig(enabled=False)
        bot.orchestrator = MagicMock()
        bot.orchestrator._command_handler = handler
        bot._channel_locks = {}
        bot._processed_messages = set()
        bot._task_threads = {}
        bot._channel = None
        bot._channel_to_project = {123: "p1"}
        bot._project_channels = {"p1": MagicMock(id=123)}
        bot._boot_time = 0.0
        bot._is_authorized = MagicMock(return_value=True)
        bot._download_attachments = AsyncMock(return_value=[])
        bot._send_long_message = AsyncMock()
        bot._safe_api_call = AsyncMock(return_value=None)

        message = MagicMock()
        message.author = MagicMock(id=42, display_name="alice", bot=False)
        message.channel = MagicMock(id=123)
        message.attachments = []
        message.content = "hi"
        message.reference = None
        message.id = 3001
        message.created_at = MagicMock()
        message.created_at.timestamp = MagicMock(return_value=1.0)
        message.add_reaction = AsyncMock()
        message.reply = AsyncMock()
        _bot_user = MagicMock(id=1)
        type(bot).user = _bot_user
        message.mentions = [_bot_user]

        await AgentQueueBot.on_message(bot, message)

        assert not any(cmd == "message_send" for cmd, _ in called)
        message.reply.assert_awaited()
        posted = message.reply.await_args.args[0]
        assert "disabled" in posted.lower()


# ---------------------------------------------------------------------------
# Bot module invariants — no in-process chat agent
# ---------------------------------------------------------------------------


def test_bot_has_no_inprocess_chat_brain():
    import inspect

    import src.discord.bot as botmod

    src_text = inspect.getsource(botmod)
    assert "self.agent" not in src_text
    assert ".chat(" not in src_text  # bot never calls Supervisor.chat


def test_routing_enabled_depends_only_on_enabled_flag():
    from types import SimpleNamespace

    from src.discord.bot import supervisor_session_routing_enabled

    cfg = SimpleNamespace(supervisor_agent=SimpleNamespace(enabled=True))
    assert supervisor_session_routing_enabled(cfg) is True
    cfg2 = SimpleNamespace(supervisor_agent=SimpleNamespace(enabled=False))
    assert supervisor_session_routing_enabled(cfg2) is False


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
        bot._safe_api_call = AsyncMock(return_value=None)
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
        """Explicit other-surface threads stay private; missing threads use the project."""
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
            # A session reply without a thread uses the configured project channel.
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

        assert bot._send_message.await_count == 1
        assert bot._send_message.await_args.args[0] == "no thread"
        assert bot._send_message.await_args.kwargs["project_id"] == "p1"
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

    async def test_renders_system_authored_parked_message_as_warning(self, db):
        """Delivery-engine parked-message notifications (from_kind="system")
        must render in the originating Discord channel as a warning embed
        so the user knows their message was dropped.
        """
        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        parked = await db.create_message(
            project_id="p1",
            from_kind="system",
            from_id="delivery-engine",
            to_kind="user",
            to_id="discord:42",
            body=(
                "[parked] your message to session:supervisor-p1 (m-orig) "
                "was not delivered within 6h. Original body:\n\nhello supervisor"
            ),
            subject="Undelivered: (no subject)",
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
                    "message_id": parked.id,
                    "project_id": "p1",
                    "from_kind": "system",
                    "from_id": "delivery-engine",
                    "to_kind": "user",
                    "to_id": "discord:42",
                    "thread_id": "discord:999",
                },
            )
        finally:
            handler.shutdown()

        # Parked warning routed through the rate-guarded channel.send path.
        assert bot._safe_api_call.await_count == 1
        call = bot._safe_api_call.await_args
        assert call.kwargs.get("context") == "message.sent parked warning"

    async def test_channel_miss_emits_warning_log(self, db, caplog):
        import logging

        from src.discord.notification_handler import DiscordNotificationHandler
        from src.event_bus import EventBus

        reply = await db.create_message(
            project_id="p1",
            from_kind="session",
            from_id="supervisor-p1",
            to_kind="user",
            to_id="discord:42",
            body="hi",
            thread_id="discord:999",
        )
        bus = EventBus(env="dev", validate_events=False)
        bot = await self._make_bot(db, channel=None)  # get_channel returns None
        handler = DiscordNotificationHandler(bot, bus)
        try:
            with caplog.at_level(logging.WARNING, logger="src.discord.agent_questions"):
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
        assert any(
            "not resolvable" in r.message and "999" in r.message for r in caplog.records
        )
