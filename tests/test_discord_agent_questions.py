"""Agent question cards and durable Discord delivery; no live transport."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.event_bus import EventBus
from src.models import Message


def question(**overrides):
    return {
        "id": "question-1", "session_id": "session-1", "session_name": "worker-pane",
        "instance_token": "original-instance", "task_id": "task-1", "project_id": "p1",
        "agent_id": "agent-1", "turn_id": "turn-1", "question": "May I deploy @everyone?",
        "requires_human": True, "state": "human", "answer": None, "answered_by": None,
        "created_at": 1.0, "updated_at": 1.0,
        "discord_channel_id": None, "discord_message_id": None, **overrides,
    }


class FakeDB:
    def __init__(self, record=None):
        self.questions = {record["id"]: record} if record else {}
        self.messages = {}
        self.receipts = {}

    async def get_agent_question(self, ident):
        return self.questions.get(ident)

    async def list_agent_questions(self, **kwargs):
        return [q for q in self.questions.values()
                if not kwargs.get("pending_only", True)
                or q["state"] in ("supervisor", "human", "answered")]

    async def mark_agent_question_notified(self, ident, channel_id, message_id):
        self.questions[ident].update(
            discord_channel_id=str(channel_id), discord_message_id=str(message_id),
        )

    async def get_message(self, ident):
        return self.messages.get(ident)

    async def get_message_discord_receipt(self, ident):
        return self.receipts.get(ident)

    async def ensure_message_discord_notification(self, ident):
        self.receipts.setdefault(ident, {
            "message_id": ident, "discord_channel_id": None, "discord_message_id": None,
        })

    async def mark_message_discord_notified(self, ident, channel_id, message_id):
        self.receipts[ident] = {
            "message_id": ident, "discord_channel_id": str(channel_id),
            "discord_message_id": str(message_id),
        }

    async def list_pending_message_discord_notifications(self, limit=100):
        return [key for key, row in self.receipts.items()
                if not row.get("discord_message_id")][:limit]


def fake_bot(db):
    sent = SimpleNamespace(id=800, channel=SimpleNamespace(id=999))
    channel = SimpleNamespace(id=999, get_partial_message=MagicMock(
        return_value=SimpleNamespace(edit=AsyncMock(return_value=sent)),
    ))

    async def safe(coro, **kwargs):
        return await coro

    return SimpleNamespace(
        orchestrator=SimpleNamespace(db=db),
        handler=SimpleNamespace(execute=AsyncMock(return_value={
            "question_id": "question-1", "state": "answered",
        })),
        _is_authorized=MagicMock(return_value=True),
        _get_channel=MagicMock(return_value=channel),
        get_channel=MagicMock(return_value=channel),
        _send_message=AsyncMock(return_value=sent),
        _send_long_message=AsyncMock(return_value=sent),
        _safe_api_call=safe,
        add_view=MagicMock(),
    )


def interaction(user_id=42):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        response=SimpleNamespace(
            send_modal=AsyncMock(), send_message=AsyncMock(), defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        message=SimpleNamespace(edit=AsyncMock()),
    )


async def reply_modal(bot):
    from src.discord.agent_questions import AgentQuestionView
    view = AgentQuestionView("question-1", bot=bot)
    click = interaction()
    await next(child for child in view.children if child.label == "Reply").callback(click)
    return view, click.response.send_modal.await_args.args[0]


async def test_authorized_reply_uses_original_question_id_and_scoped_command_only():
    bot = fake_bot(FakeDB(question()))
    view, modal = await reply_modal(bot)
    assert view.timeout is None
    assert view.is_persistent()
    assert view.children[0].custom_id == "aq:question:reply:question-1"
    modal.answer._value = "Please keep it local."
    submit = interaction()
    await modal.on_submit(submit)
    bot.handler.execute.assert_awaited_once_with("question_answer", {
        "question_id": "question-1", "body": "Please keep it local.", "_scope": {"kind": "local"},
    })
    assert submit.followup.send.await_args.kwargs["ephemeral"] is True
    assert "queued" in submit.followup.send.await_args.args[0].lower()


async def test_unauthorized_button_callback_is_rejected_even_without_framework_check():
    from src.discord.agent_questions import AgentQuestionView
    bot = fake_bot(FakeDB(question()))
    bot._is_authorized.return_value = False
    view = AgentQuestionView("question-1", bot=bot)
    click = interaction(7)
    await view.children[0].callback(click)
    click.response.send_modal.assert_not_awaited()
    click.response.send_message.assert_awaited_once()
    assert click.response.send_message.await_args.kwargs["ephemeral"] is True
    bot.handler.execute.assert_not_awaited()


async def test_modal_rechecks_authorization_after_permission_changes():
    bot = fake_bot(FakeDB(question()))
    _, modal = await reply_modal(bot)
    bot._is_authorized.return_value = False
    modal.answer._value = "An unauthorized answer"
    submit = interaction()
    await modal.on_submit(submit)
    bot.handler.execute.assert_not_awaited()
    submit.response.send_message.assert_awaited_once()


async def test_missing_authorization_configuration_fails_closed():
    from src.discord.agent_questions import AgentQuestionView
    bot = fake_bot(FakeDB(question()))
    del bot._is_authorized
    view = AgentQuestionView("question-1", bot=bot)
    click = interaction()
    assert await view.interaction_check(click) is False
    click.response.send_message.assert_awaited_once()


@pytest.mark.parametrize("error", ["question is stale", "question already answered"])
async def test_stale_or_duplicate_answer_surfaces_service_rejection(error):
    bot = fake_bot(FakeDB(question()))
    bot.handler.execute.return_value = {"error": error}
    _, modal = await reply_modal(bot)
    modal.answer._value = "Do it."
    submit = interaction()
    await modal.on_submit(submit)
    assert error in submit.followup.send.await_args.args[0]
    assert [call.args[0] for call in bot.handler.execute.await_args_list] == ["question_answer"]


async def test_card_has_literal_question_and_exact_provenance_without_mentions():
    from src.discord.notification_handler import DiscordNotificationHandler
    q = question(question="Should I run **delete** on @everyone's folder?")
    db = FakeDB(q)
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    try:
        await bus.emit("agent.question", q)
    finally:
        handler.shutdown()
    call = bot._send_message.await_args
    assert call.kwargs["project_id"] == "p1"
    assert call.kwargs["allowed_mentions"].to_dict()["parse"] == []
    embed = call.kwargs["embed"].to_dict()
    assert r"\*\*delete\*\*" in embed["description"]
    text = str(embed)
    for ident in ["agent-1", "task-1", "session-1", "worker-pane", "question-1"]:
        assert ident in text
    assert "original-instance" not in text
    assert q["discord_channel_id"] == "999"
    assert q["discord_message_id"] == "800"


async def test_question_duplicate_events_and_restart_do_not_resend():
    from src.discord.notification_handler import DiscordNotificationHandler
    from src.discord.agent_questions import restore_agent_question_views
    db = FakeDB(question())
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    await asyncio.gather(bus.emit("agent.question", question()), bus.emit("agent.question", question()))
    handler.shutdown()
    assert bot._send_message.await_count == 1
    restarted = fake_bot(db)
    await restore_agent_question_views(restarted)
    restored_view = restarted.add_view.call_args.args[0]
    assert restored_view.is_persistent()
    assert restarted.add_view.call_args.kwargs["message_id"] == 800
    restarted_handler = DiscordNotificationHandler(restarted, bus)
    try:
        await bus.emit("agent.question", question())
    finally:
        restarted_handler.shutdown()
    restarted._send_message.assert_not_awaited()
    click = interaction()
    await restored_view.children[0].callback(click)
    modal = click.response.send_modal.await_args.args[0]
    modal.answer._value = "Reply after restart"
    await modal.on_submit(interaction())
    assert restarted.handler.execute.await_args.args[1]["question_id"] == "question-1"


async def test_failed_question_send_remains_unnotified_and_retries_after_restart():
    from src.discord.notification_handler import DiscordNotificationHandler
    db = FakeDB(question())
    bot = fake_bot(db)
    bot._send_message.return_value = None
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    await bus.emit("agent.question", question())
    handler.shutdown()
    assert db.questions["question-1"]["discord_message_id"] is None
    restarted = fake_bot(db)
    handler = DiscordNotificationHandler(restarted, bus)
    try:
        await bus.emit("agent.question", question())
    finally:
        handler.shutdown()
    assert restarted._send_message.await_count == 1
    assert db.questions["question-1"]["discord_message_id"] == "800"


@pytest.mark.parametrize("state", ["supervisor", "answered", "delivered", "resolved", "stale"])
async def test_late_question_event_cannot_post_a_closed_or_nonhuman_question(state):
    from src.discord.notification_handler import DiscordNotificationHandler
    db = FakeDB(question(state=state))
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    try:
        await bus.emit("agent.question", question())
    finally:
        handler.shutdown()
    bot._send_message.assert_not_awaited()


async def test_answer_update_disables_card_without_resending():
    from src.discord.notification_handler import DiscordNotificationHandler
    db = FakeDB(question(discord_channel_id="999", discord_message_id="800", state="answered"))
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    try:
        await bus.emit("agent.question.updated", db.questions["question-1"])
    finally:
        handler.shutdown()
    edited = bot.get_channel.return_value.get_partial_message.return_value.edit.await_args.kwargs
    assert all(child.disabled for child in edited["view"].children)
    assert "queued" in str(edited["embed"].to_dict()).lower()
    bot._send_message.assert_not_awaited()


def user_message(**overrides):
    return Message(
        id="message-1", project_id="p1", from_kind="session", from_id="session-1",
        to_kind="user", to_id="user", body="I need your input @everyone.",
        **overrides,
    )


async def test_session_message_without_thread_reaches_project_once_for_both_events_and_restart():
    from src.discord.notification_handler import DiscordNotificationHandler
    db = FakeDB()
    db.messages["message-1"] = user_message()
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    event = {"message_id": "message-1", "project_id": "p1", "from_kind": "session", "to_kind": "user"}
    await asyncio.gather(bus.emit("message.sent", event), bus.emit("message.delivered", event))
    await bus.emit("message.sent", event)
    handler.shutdown()
    assert bot._send_message.await_count == 1
    assert bot._send_message.await_args.kwargs["project_id"] == "p1"
    assert bot._send_message.await_args.kwargs["allowed_mentions"].to_dict()["parse"] == []
    assert db.receipts["message-1"]["discord_message_id"] == "800"
    restarted = fake_bot(db)
    handler = DiscordNotificationHandler(restarted, bus)
    try:
        await bus.emit("message.sent", event)
    finally:
        handler.shutdown()
    restarted._send_message.assert_not_awaited()


async def test_failed_user_message_retries_from_enrolled_outbox_after_restart():
    from src.discord.notification_handler import DiscordNotificationHandler
    from src.discord.agent_questions import retry_discord_user_messages
    db = FakeDB()
    db.messages["message-1"] = user_message()
    bot = fake_bot(db)
    bot._send_message.return_value = None
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    await bus.emit("message.sent", {"message_id": "message-1"})
    handler.shutdown()
    assert not db.receipts["message-1"]["discord_message_id"]
    restarted = fake_bot(db)
    await retry_discord_user_messages(restarted)
    assert restarted._send_message.await_count == 1
    assert db.receipts["message-1"]["discord_message_id"] == "800"


async def test_retry_does_not_replay_historical_messages_without_outbox_enrollment():
    from src.discord.agent_questions import retry_discord_user_messages
    db = FakeDB()
    db.messages["message-1"] = user_message(delivered_at=2.0)
    bot = fake_bot(db)
    await retry_discord_user_messages(bot)
    bot._send_message.assert_not_awaited()


@pytest.mark.parametrize("kind,sender,thread", [
    ("user", "discord:1", "discord:999"),
    ("system", "unrelated-subsystem", "discord:999"),
    ("system", "delivery-engine", None),
    ("session", "session-1", "slack:channel"),
])
async def test_other_surfaces_and_unrelated_system_messages_do_not_become_parked_warnings(kind,sender,thread):
    from src.discord.notification_handler import DiscordNotificationHandler
    db = FakeDB()
    db.messages["m"] = Message(
        id="m", project_id="p1", from_kind=kind, from_id=sender,
        to_kind="user", to_id="user", body="not a parked warning", thread_id=thread,
    )
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    try:
        await bus.emit("message.sent", {"message_id": "m", "project_id": "p1",
            "from_kind": kind, "from_id": sender, "to_kind": "user", "thread_id": thread})
    finally:
        handler.shutdown()
    bot._send_message.assert_not_awaited()
    bot._send_long_message.assert_not_awaited()


@pytest.mark.parametrize("body", ["Short @everyone question", "long body\n" * 400], ids=["short", "attachment"])
async def test_single_discord_delivery_returns_real_message_and_never_splits(body):
    from src.discord.bot import AgentQueueBot
    sent = SimpleNamespace(id=123, channel=SimpleNamespace(id=999))
    channel = SimpleNamespace(id=999, send=AsyncMock(return_value=sent))
    transport = SimpleNamespace(_safe_api_call=fake_bot(FakeDB())._safe_api_call)
    result = await AgentQueueBot._send_long_message(
        transport, channel, body, single_message=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    assert result is sent
    channel.send.assert_awaited_once()
    sent_kwargs = channel.send.await_args.kwargs
    assert sent_kwargs["allowed_mentions"].to_dict()["parse"] == []
    if len(body) > 2000:
        assert sent_kwargs["file"].fp.getvalue().decode() == body


async def test_question_card_transport_suppresses_mentions_and_attaches_full_long_question():
    from src.discord.bot import AgentQueueBot
    sent = SimpleNamespace(id=123, channel=SimpleNamespace(id=999))
    channel = SimpleNamespace(id=999, send=AsyncMock(return_value=sent))
    transport = SimpleNamespace(
        _get_channel=lambda _project: channel,
        _is_global_channel=lambda *_args: False,
        _safe_api_call=fake_bot(FakeDB())._safe_api_call,
    )
    attached = discord.File(__import__("io").BytesIO(b"full body"), filename="question.txt")
    result = await AgentQueueBot._send_message(
        transport, "question", project_id="p1", embed=discord.Embed(description="preview"),
        file=attached, allowed_mentions=discord.AllowedMentions.none(),
    )
    assert result is sent
    assert channel.send.await_args.kwargs["file"] is attached
    assert channel.send.await_args.kwargs["allowed_mentions"].to_dict()["parse"] == []


async def test_bot_ready_restores_persistent_views_and_starts_one_retry_loop(monkeypatch):
    from src.discord.bot import AgentQueueBot
    from src.discord import agent_questions
    restore = AsyncMock()
    retry = AsyncMock()
    monkeypatch.setattr(agent_questions, "restore_agent_question_views", restore)
    monkeypatch.setattr(agent_questions, "retry_discord_user_messages", retry)
    bot = fake_bot(FakeDB())
    bot.user = "test-bot"
    bot.config = SimpleNamespace(discord=SimpleNamespace(guild_id=None))
    bot.orchestrator.bus = SimpleNamespace(emit=AsyncMock())
    loop = MagicMock()
    loop.is_running.return_value = False
    loop.start.side_effect = lambda: setattr(loop.is_running, "return_value", True)
    bot._retry_discord_user_notifications = loop
    await AgentQueueBot.on_ready(bot)
    await AgentQueueBot.on_ready(bot)
    assert restore.await_count == 2
    assert retry.await_count == 2
    loop.start.assert_called_once()


@pytest.mark.parametrize("kind", ["question", "message"])
async def test_database_ack_retry_in_same_process_never_sends_twice(kind):
    from src.discord.notification_handler import DiscordNotificationHandler
    db = FakeDB(question())
    db.messages["message-1"] = user_message()
    method = "mark_agent_question_notified" if kind == "question" else "mark_message_discord_notified"
    original = getattr(db, method)
    attempts = 0

    async def acknowledge(*args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Temporary DB outage after Discord accepted the message")
        return await original(*args)

    setattr(db, method, acknowledge)
    bot = fake_bot(db)
    bus = EventBus(validate_events=False)
    handler = DiscordNotificationHandler(bot, bus)
    event, payload = ("agent.question", question()) if kind == "question" else (
        "message.sent", {"message_id": "message-1"},
    )
    try:
        if kind == "question":
            # The service catches subscriber errors and retries notification.
            with pytest.raises(RuntimeError, match="Temporary DB outage"):
                await bus.emit(event, payload)
        else:
            await bus.emit(event, payload)
        await bus.emit(event, payload)
    finally:
        handler.shutdown()
    assert attempts == 2
    assert bot._send_message.await_count == 1
    receipt = db.questions["question-1"] if kind == "question" else db.receipts["message-1"]
    assert receipt["discord_message_id"] == "800"
