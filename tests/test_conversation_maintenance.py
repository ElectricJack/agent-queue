"""Delay notices, retained dedup provenance and idle conversation closing."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import update

from src.commands.handler import CommandHandler
from src.commands.principal import ExecutionPrincipal, principal_context
from src.config import AppConfig
from src.conversations.maintenance import ConversationMaintenance
from src.conversations.outbox import RecordingOutbox, UnboundOutbox
from src.database import Database
from src.database.tables import messages
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

DAY = 86400
NOW = 100 * DAY
AUTHOR = "333333333333333333"
CHANNEL = "222222222222222222"
GUILD = "111111111111111111"
THREAD = "555555555555555555"


@pytest.fixture
async def db():
    database = Database(lease_dsn("conversation-maintenance"))
    await database.initialize()
    yield database
    await database.close()


async def accept(db, *, external_id="900", received_at=NOW - 901, conversation_id=None):
    return await db.accept_conversation_input(
        transport="discord",
        guild_id=GUILD,
        channel_id=CHANNEL,
        external_message_id=external_id,
        external_root_message_id=external_id,
        external_thread_id=None,
        author_id=AUTHOR,
        verified_actor=f"human:discord:{AUTHOR}",
        text="hello supervisor",
        audience=[AUTHOR],
        source="test",
        received_at=received_at,
        conversation_id=conversation_id,
        brief="hello supervisor",
        now=received_at,
    )


def result(**overrides):
    return {
        "delay_notices": 0,
        "expired_text": 0,
        "deleted_tombstones": 0,
        "closed": 0,
        **overrides,
    }


async def test_delay_notice_once_per_input_at_900_seconds_and_never_for_delivered(db):
    first = await accept(db)
    boundary = await accept(db, external_id="901", received_at=NOW - 900)
    recent = await accept(db, external_id="902", received_at=NOW - 899)
    delivered = await accept(db, external_id="903")
    await db.mark_delivered(delivered["supervisor_message_id"])
    outbox = RecordingOutbox()
    sweep = ConversationMaintenance(db=db, outbox=outbox, clock=lambda: NOW)

    assert await sweep.tick() == result(delay_notices=2)
    assert await ConversationMaintenance(db=db, outbox=outbox).tick(now=NOW) == result()
    assert [row["dedup_key"] for row in outbox.rows] == [
        f"conv-notice:delay:{item['input']['id']}" for item in (first, boundary)
    ]
    for item in (first, boundary):
        stored = await db.get_conversation_input(item["input"]["id"])
        assert (stored["state"], stored["delay_notified_at"]) == ("accepted", NOW)
        row = outbox.by_key(f"conv-notice:delay:{stored['id']}")
        assert row["owner_id"] == stored["conversation_id"]
        assert row["kind"] == "notice"
        assert row["payload"]["kind"] == "delay"
        assert row["payload"]["conversation_id"] == stored["conversation_id"]
        assert "still queued" in row["payload"]["text"]
    for item in (recent, delivered):
        assert (await db.get_conversation_input(item["input"]["id"]))["delay_notified_at"] is None


async def test_crash_after_enqueue_retries_same_notice_before_stamping(db, monkeypatch):
    item = await accept(db)
    outbox = RecordingOutbox()
    sweep = ConversationMaintenance(db=db, outbox=outbox)
    stamp = db.mark_delay_notified
    with monkeypatch.context() as patch:
        patch.setattr(db, "mark_delay_notified", AsyncMock(side_effect=RuntimeError("crash")))
        with pytest.raises(RuntimeError, match="crash"):
            await sweep.tick(now=NOW)
    assert len(outbox.rows) == 1
    assert (await db.get_conversation_input(item["input"]["id"]))["delay_notified_at"] is None
    assert await sweep.tick(now=NOW) == result(delay_notices=1)
    assert len(outbox.rows) == 1
    assert not await stamp(item["input"]["id"], now=NOW)


async def test_failed_enqueue_does_not_stamp_input(db):
    item = await accept(db)
    outbox = RecordingOutbox()
    outbox.enqueue = AsyncMock(side_effect=RuntimeError("outage"))
    with pytest.raises(RuntimeError, match="outage"):
        await ConversationMaintenance(db=db, outbox=outbox).tick(now=NOW)
    assert (await db.get_conversation_input(item["input"]["id"]))["delay_notified_at"] is None


async def test_text_expires_but_hash_and_external_id_still_deduplicate(db):
    item = await accept(db, received_at=NOW - 31 * DAY)
    sweep = ConversationMaintenance(db=db, outbox=UnboundOutbox())
    assert await sweep.tick(now=NOW) == result(expired_text=1)
    stored = await db.get_conversation_input(item["input"]["id"])
    assert stored["text"] is None
    assert stored["text_sha256"] == item["input"]["text_sha256"]
    assert (stored["state"], stored["text_expired_at"]) == ("expired", NOW)
    assert (await db.get_message(item["supervisor_message_id"])).archived_at is not None
    replay = await accept(db)
    assert replay["created"] is False
    assert replay["input"]["id"] == stored["id"]
    assert await sweep.tick(now=NOW) == result()


async def test_only_after_90_days_can_external_id_be_accepted_again(db):
    item = await accept(db, received_at=NOW - 90 * DAY)
    sweep = ConversationMaintenance(db=db, outbox=UnboundOutbox())
    assert await sweep.tick(now=NOW) == result(expired_text=1)
    assert (await accept(db))["created"] is False
    assert await sweep.tick(now=NOW + DAY) == result(deleted_tombstones=1)
    assert await db.get_conversation_input(item["input"]["id"]) is None
    replay = await accept(db, received_at=NOW + DAY)
    assert replay["created"] is True
    assert replay["input"]["id"] != item["input"]["id"]


async def test_retention_archives_old_thread_messages_only_and_preserves_boundary(db):
    old = await accept(db, received_at=NOW - 31 * DAY)
    boundary = await accept(db, external_id="901", received_at=NOW - 30 * DAY)
    fresh = await accept(db, external_id="902", received_at=NOW)
    reply = await db.record_conversation_reply(
        conversation_id=old["conversation"]["id"],
        input_id=old["input"]["id"],
        reply_message_id="msg-old-reply",
        body="answer",
        now=NOW - 31 * DAY,
    )
    second_reply = await db.record_conversation_reply(
        conversation_id=old["conversation"]["id"],
        input_id=old["input"]["id"],
        reply_message_id="msg-old-reply-2",
        body="second answer",
        now=NOW - 31 * DAY + 1,
    )
    unrelated = await db.create_message(
        project_id=None,
        from_kind="user",
        from_id="dashboard",
        to_kind="session",
        to_id="supervisor-global",
        body="unrelated",
    )
    async with db._engine.begin() as conn:
        await conn.execute(
            update(messages).where(messages.c.id == unrelated.id).values(created_at=NOW - 31 * DAY)
        )
    assert await ConversationMaintenance(db=db, outbox=UnboundOutbox()).tick(now=NOW) == result(
        expired_text=1
    )
    for message_id in (
        old["supervisor_message_id"],
        reply["message"]["id"],
        second_reply["message"]["id"],
    ):
        assert (await db.get_message(message_id)).archived_at is not None
    for message_id in (
        boundary["supervisor_message_id"],
        fresh["supervisor_message_id"],
        unrelated.id,
    ):
        assert (await db.get_message(message_id)).archived_at is None
    assert (await db.get_conversation_input(boundary["input"]["id"]))["text"] is not None


async def test_sweep_drains_bounded_delay_and_archive_batches(db, monkeypatch):
    for index in range(3):
        await accept(db, external_id=str(index), received_at=NOW - 31 * DAY)
    list_inputs = db.list_inputs_awaiting_supervisor
    list_message_ids = db.list_expired_conversation_message_ids

    async def input_page(**kwargs):
        return await list_inputs(**kwargs, limit=1)

    async def message_page(**kwargs):
        return await list_message_ids(**kwargs, limit=1)

    inputs = AsyncMock(side_effect=input_page)
    message_ids = AsyncMock(side_effect=message_page)
    monkeypatch.setattr(db, "list_inputs_awaiting_supervisor", inputs)
    monkeypatch.setattr(db, "list_expired_conversation_message_ids", message_ids)
    outbox = RecordingOutbox()
    assert await ConversationMaintenance(db=db, outbox=outbox).tick(now=NOW) == result(
        delay_notices=3, expired_text=3
    )
    assert inputs.await_count == message_ids.await_count == 4
    assert len(outbox.rows) == 3
    assert await list_message_ids(older_than=NOW) == []


@pytest.mark.parametrize("state", ["open", "delivery_blocked"])
async def test_idle_closing_followup_posts_one_closed_notice(db, state):
    item = await accept(db, external_id="900000000000000000", received_at=NOW - 31 * DAY)
    conv_id = item["conversation"]["id"]
    await db.bind_conversation_thread(conv_id, external_thread_id=THREAD, now=NOW - 31 * DAY)
    await db.set_conversation_state(conv_id, state=state, now=NOW - 31 * DAY)
    outbox = RecordingOutbox()
    assert await ConversationMaintenance(db=db, outbox=outbox).tick(now=NOW) == result(
        delay_notices=1, expired_text=1, closed=1
    )
    conversation = await db.get_conversation(conv_id)
    assert (conversation["state"], conversation["closed_at"]) == ("closed", NOW)
    config = AppConfig()
    config.discord.conversation.enabled = True
    config.discord.guild_id, config.discord.channel_id = GUILD, CHANNEL
    config.discord.authorized_users = [AUTHOR]
    config.messages.enabled = config.sessions.enabled = True
    orch = SimpleNamespace(
        db=db,
        config=config,
        conversation_outbox=outbox,
        _discord_bot=SimpleNamespace(_cutover_report=SimpleNamespace(status="complete")),
        bus=SimpleNamespace(emit=AsyncMock()),
    )
    handler = CommandHandler(orch, config)
    handler._clock = lambda: NOW
    envelope = dict(
        transport="discord",
        guild_id=GUILD,
        channel_id=CHANNEL,
        external_message_id="900000000000000001",
        external_root_message_id="900000000000000000",
        external_thread_id=THREAD,
        author_id=AUTHOR,
        text="are you there?",
        received_at=NOW,
        mentions_bot=False,
    )
    with principal_context(ExecutionPrincipal.service("discord-gateway")):
        for _ in range(2):
            response = await handler.execute(
                "supervisor_inbox_post", {"envelope": envelope, "source": "gateway"}
            )
            assert response["error_code"] == "conversation_closed"
    assert (
        outbox.by_key(f"conv-notice:closed:{conv_id}")["payload"]["kind"] == "conversation_closed"
    )
    assert len(outbox.rows) == 2


async def test_new_input_keeps_conversation_open_and_opening_is_not_closed(db):
    old = await accept(db, received_at=NOW - 31 * DAY)
    await db.bind_conversation_thread(
        old["conversation"]["id"], external_thread_id=THREAD, now=NOW - 31 * DAY
    )
    await accept(db, external_id="901", received_at=NOW, conversation_id=old["conversation"]["id"])
    opening = await accept(db, external_id="902", received_at=NOW - 31 * DAY)
    await ConversationMaintenance(db=db, outbox=UnboundOutbox()).tick(now=NOW)
    assert (await db.get_conversation(old["conversation"]["id"]))["state"] == "open"
    assert (await db.get_conversation(opening["conversation"]["id"]))["state"] == "opening"


async def test_hourly_orchestrator_hook_uses_current_binding_and_existing_rows(monkeypatch, caplog):
    harness = SimpleNamespace(
        db=SimpleNamespace(list_conversations=AsyncMock(return_value=[])),
        conversation_outbox=UnboundOutbox(),
        _last_conversation_maintenance=0.0,
    )
    tick = AsyncMock(return_value=result())
    monkeypatch.setattr(ConversationMaintenance, "tick", tick)
    await Orchestrator._maintain_conversations(harness, now=NOW)
    tick.assert_not_awaited()
    harness.db.list_conversations.return_value = [{"id": "conv-existing"}]
    await Orchestrator._maintain_conversations(harness, now=NOW + 3600)
    tick.assert_awaited_once_with(now=NOW + 3600)
    await Orchestrator._maintain_conversations(harness, now=NOW + 3601)
    assert tick.await_count == 1
    harness.conversation_outbox = RecordingOutbox()
    harness.db.list_conversations.return_value = []
    tick.side_effect = RuntimeError("maintenance failed")
    await Orchestrator._maintain_conversations(harness, now=NOW + 7200)
    assert tick.await_count == 2
    assert "Conversation maintenance failed" in caplog.text
