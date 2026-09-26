"""Bounded reconnect recovery, durable page cursors and accepted provenance."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from src.config import AppConfig
from src.conversations.backfill import ConversationBackfill
from src.conversations.preconditions import conversation_preconditions
from src.discord.bot import AgentQueueBot
from src.discord.escalation_transport import DiscordEscalationTransport
from src.escalations.transport import TransportRetryable, TransportUnavailable

NOW = datetime(2026, 9, 25, tzinfo=UTC).timestamp()
DAY = 86400
CHANNEL = "222222222222222222"
THREAD = "555555555555555555"
BOT = 444444444444444444


def config():
    value = AppConfig()
    value.discord.conversation.enabled = True
    value.discord.guild_id = "111111111111111111"
    value.discord.channel_id = CHANNEL
    value.discord.authorized_users = ["333333333333333333"]
    value.messages.enabled = value.sessions.enabled = True
    return value


def message(index, *, timestamp=None):
    return SimpleNamespace(
        id=900000000000000000 + index,
        created_at=datetime.fromtimestamp(timestamp or NOW - 2000 + index, UTC),
    )


class MemoryDB:
    def __init__(self, conversations=()):
        self.cursors = {}
        self.advances = []
        self.gaps = []
        self.conversations = list(conversations)

    async def list_conversations(self, **kwargs):
        assert kwargs["states"] == ["open"]
        return self.conversations

    async def get_backfill_cursor(self, *, channel_id, **kwargs):
        return self.cursors.get(channel_id)

    async def advance_backfill_cursor(self, *, channel_id, **kwargs):
        self.advances.append((channel_id, kwargs["last_external_message_id"]))
        self.cursors[channel_id] = {
            "last_external_message_id": kwargs["last_external_message_id"],
            "advanced_at": kwargs["now"],
        }

    async def record_intake_gap(self, **kwargs):
        self.gaps.append(kwargs)
        return kwargs


class History:
    def __init__(self, rows=None, forbidden=()):
        self.rows = rows or {}
        self.forbidden = forbidden
        self.calls = []

    async def read_history(self, **kwargs):
        self.calls.append(kwargs)
        channel_id = kwargs["channel_id"]
        if channel_id in self.forbidden:
            raise TransportUnavailable("no history permission")
        after = int(kwargs["after_message_id"] or 0)
        return [
            row
            for row in self.rows.get(channel_id, [])
            if row.id > after
            and kwargs["after_ts"] < row.created_at.timestamp() < kwargs["before_ts"]
        ][: kwargs["limit"]]


def setup(*, count=0, db=None, cfg=None, transport=None, page_size=100):
    cfg = cfg or config()
    db = db or MemoryDB()
    transport = transport or History({CHANNEL: [message(i) for i in range(count)]})
    router = SimpleNamespace(
        preconditions=lambda: conversation_preconditions(
            cfg, cutover_status="complete", outbox_bound=True
        ),
        route=AsyncMock(return_value="posted:created"),
    )
    backfill = ConversationBackfill(
        db=db,
        router=router,
        transport=transport,
        config=cfg,
        clock=lambda: NOW,
        page_size=page_size,
    )
    return backfill, db, router, transport


async def test_250_messages_advance_only_after_each_fully_routed_page():
    backfill, db, router, transport = setup(count=250)

    async def route(row, **kwargs):
        assert kwargs == {"bot_user_id": BOT, "source": "backfill"}
        assert len(db.advances) == (row.id - message(0).id) // 100
        return "posted:created"

    router.route.side_effect = route
    assert await backfill.run(bot_user_id=BOT) == {
        "channels": 1,
        "messages": 250,
        "gaps": [],
        "skipped": None,
    }
    assert db.advances == [(CHANNEL, str(message(i).id)) for i in (99, 199, 249)]
    assert transport.calls[0]["after_ts"] == NOW - DAY
    assert transport.calls[1]["after_message_id"] == str(message(99).id)


async def test_pass_cap_records_gap_and_next_pass_resumes():
    backfill, db, router, _ = setup(count=1100, page_size=300)
    first = await backfill.run(bot_user_id=BOT)
    assert first["messages"] == router.route.await_count == 1000
    assert [gap["reason"] for gap in first["gaps"]] == ["pass_cap"]
    assert first["gaps"][0]["gap_from"] == message(999).created_at.timestamp()
    assert db.cursors[CHANNEL]["last_external_message_id"] == str(message(999).id)
    assert (await backfill.run(bot_user_id=BOT))["messages"] == 100


async def test_stale_cursor_records_expired_gap_and_reads_only_24_hours():
    backfill, db, _, transport = setup(count=1)
    db.cursors[CHANNEL] = {"last_external_message_id": "42", "advanced_at": NOW - 2 * DAY}
    result = await backfill.run(bot_user_id=BOT)
    assert result["messages"] == 1
    assert result["gaps"] == [
        {
            "transport": "discord",
            "channel_id": CHANNEL,
            "gap_from": NOW - 2 * DAY,
            "gap_to": NOW - DAY,
            "reason": "cursor_expired",
            "now": NOW,
        }
    ]
    assert transport.calls[0]["after_message_id"] is None
    assert transport.calls[0]["after_ts"] == NOW - DAY


async def test_forbidden_history_records_gap_and_continues_with_active_threads():
    db = MemoryDB(
        [
            {
                "transport": "discord",
                "guild_id": config().discord.guild_id,
                "channel_id": CHANNEL,
                "external_thread_id": THREAD,
            }
        ]
    )
    transport = History({THREAD: [message(1)]}, forbidden=[CHANNEL])
    backfill, _, _, _ = setup(db=db, transport=transport)
    result = await backfill.run(bot_user_id=BOT)
    assert result["channels"] == 2 and result["messages"] == 1
    assert result["gaps"][0]["reason"] == "history_forbidden"
    assert result["gaps"][0]["gap_from"] == NOW - DAY
    assert CHANNEL not in db.cursors and THREAD in db.cursors


@pytest.mark.parametrize(
    "mode,code",
    [
        ("disabled", "conversation_disabled"),
        ("allowlist", "empty_allowlist"),
    ],
)
async def test_unmet_preconditions_skip_all_reads(mode, code):
    cfg = config()
    if mode == "disabled":
        cfg.discord.conversation.enabled = False
    else:
        cfg.discord.authorized_users = []
    backfill, db, router, transport = setup(cfg=cfg, count=1)
    assert await backfill.run(bot_user_id=BOT) == {
        "channels": 0,
        "messages": 0,
        "gaps": [],
        "skipped": code,
    }
    assert transport.calls == db.advances == []
    router.route.assert_not_awaited()


@pytest.mark.parametrize(
    "outcome",
    [
        "refused:post_failed",
        "escalation_failed",
        "ignored:classify_error",
        "ignored:preconditions_unmet",
        "refused:preconditions_unmet",
    ],
)
async def test_failed_page_is_not_advanced_and_retries(outcome):
    backfill, db, router, _ = setup(count=3)
    router.route.side_effect = ["posted:created", outcome]
    with pytest.raises(RuntimeError, match="not persisted"):
        await backfill.run(bot_user_id=BOT)
    assert db.advances == []
    router.route.side_effect = None
    assert (await backfill.run(bot_user_id=BOT))["messages"] == 3


async def test_retryable_transport_error_does_not_record_a_false_permission_gap():
    backfill, db, _, transport = setup(count=1)
    transport.read_history = AsyncMock(side_effect=TransportRetryable("429"))
    with pytest.raises(TransportRetryable):
        await backfill.run(bot_user_id=BOT)
    assert db.gaps == db.advances == []


async def test_failure_in_second_page_keeps_first_page_cursor():
    backfill, db, router, _ = setup(count=4, page_size=2)
    router.route.side_effect = [
        "posted:created",
        "posted:created",
        "posted:created",
        "refused:post_failed",
    ]
    with pytest.raises(RuntimeError, match="not persisted"):
        await backfill.run(bot_user_id=BOT)
    assert db.advances == [(CHANNEL, str(message(1).id))]
    router.route.side_effect = None
    assert (await backfill.run(bot_user_id=BOT))["messages"] == 2


async def test_pass_cap_is_shared_across_targets_and_records_unvisited_thread_gap():
    db = MemoryDB(
        [
            {
                "transport": "discord",
                "guild_id": config().discord.guild_id,
                "channel_id": CHANNEL,
                "external_thread_id": THREAD,
            }
        ]
    )
    transport = History(
        {
            CHANNEL: [message(i) for i in range(1000)],
            THREAD: [message(1001)],
        }
    )
    backfill, _, router, _ = setup(db=db, transport=transport)
    result = await backfill.run(bot_user_id=BOT)
    assert result["messages"] == router.route.await_count == 1000
    assert [gap["channel_id"] for gap in result["gaps"]] == [CHANNEL, THREAD]
    assert result["gaps"][1]["gap_from"] == NOW - DAY
    assert THREAD not in db.cursors
    assert {call["channel_id"] for call in transport.calls} == {CHANNEL}


async def test_recovery_covers_more_than_50_active_threads_and_filters_foreign_bindings():
    threads = [str(int(THREAD) + i) for i in range(55)]
    rows = [
        {
            "transport": "discord",
            "guild_id": config().discord.guild_id,
            "channel_id": CHANNEL,
            "external_thread_id": thread,
        }
        for thread in threads
    ]
    rows.extend([{**rows[0], "channel_id": "foreign"}, {**rows[0], "external_thread_id": None}])
    backfill, _, _, transport = setup(db=MemoryDB(rows))
    result = await backfill.run(bot_user_id=BOT)
    assert result["channels"] == 56
    assert [call["channel_id"] for call in transport.calls] == [CHANNEL, *threads]


def test_diagnostics_reports_intent_and_cached_permissions_without_network():
    backfill, _, _, _ = setup()
    permissions = SimpleNamespace(
        view_channel=True,
        read_message_history=False,
        send_messages=True,
        create_public_threads=False,
        send_messages_in_threads=True,
    )
    bot = SimpleNamespace(
        intents=SimpleNamespace(message_content=False),
        get_channel=lambda _: SimpleNamespace(
            guild=SimpleNamespace(me=object()),
            permissions_for=lambda _: permissions,
        ),
    )
    assert backfill.diagnostics(bot) == {
        "message_content_intent": False,
        "permissions": vars(permissions),
    }
    bot.get_channel = lambda _: None
    assert backfill.diagnostics(bot)["permissions"] is None


@pytest.mark.parametrize("event", ["on_ready", "on_resumed"])
async def test_gateway_reconnect_runs_backfill_after_cutover(monkeypatch, event, caplog):
    bot = AgentQueueBot.__new__(AgentQueueBot)
    bot.config = config()
    bot.orchestrator = SimpleNamespace()
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=BOT))
    bot.get_guild = lambda _: None
    bot._cutover_complete = asyncio.Event()
    report = SimpleNamespace(status="complete", summary=lambda: "cutover complete")
    bot._cutover_report = report
    monkeypatch.setattr("src.discord.cutover.run_discord_cutover", AsyncMock(return_value=report))
    run = AsyncMock(side_effect=RuntimeError("test failure"))
    bot._conversation_backfill = lambda: SimpleNamespace(run=run)
    await getattr(bot, event)()
    run.assert_awaited_once_with(bot_user_id=BOT)
    assert "backfill" in caplog.text
    if event == "on_ready":
        assert bot._cutover_complete.is_set()


async def test_history_adapter_pages_oldest_first_with_window_and_maps_forbidden():
    calls = []

    async def history(**kwargs):
        calls.append(kwargs)
        yield message(1)

    channel = SimpleNamespace(history=history, send=AsyncMock())
    bot = SimpleNamespace(get_channel=lambda _: channel)
    transport = DiscordEscalationTransport(bot, config())
    assert await transport.read_history(
        channel_id=CHANNEL,
        after_message_id=None,
        after_ts=NOW - DAY,
        before_ts=NOW,
        limit=100,
    )
    assert calls[0]["oldest_first"] is True and calls[0]["limit"] == 100
    assert calls[0]["after"].id == discord.utils.time_snowflake(
        datetime.fromtimestamp(NOW - DAY, UTC),
        high=True,
    )

    cursor = discord.utils.time_snowflake(datetime.fromtimestamp(NOW - 100, UTC), high=True)
    await transport.read_history(
        channel_id=CHANNEL,
        after_message_id=str(cursor),
        after_ts=NOW - DAY,
        before_ts=NOW,
        limit=100,
    )
    assert calls[-1]["after"].id == cursor
    assert calls[-1]["before"] == datetime.fromtimestamp(NOW, UTC)

    async def forbidden(**kwargs):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no access")
        yield  # pragma: no cover

    channel.history = forbidden
    with pytest.raises(TransportUnavailable):
        await transport.read_history(
            channel_id=CHANNEL,
            after_message_id=None,
            after_ts=NOW - DAY,
            before_ts=NOW,
            limit=100,
        )


@pytest.fixture
async def real_ingress():
    from src.commands.handler import CommandHandler
    from src.conversations.outbox import RecordingOutbox
    from src.database import Database
    from src.discord.escalation_intake import DiscordEscalationIntake
    from src.discord.inbound import DiscordInboundRouter
    from src.discord.intake_diagnostics import IgnoreCounter
    from tests.db_fixtures import lease_dsn

    db = Database(lease_dsn("conversation-backfill"))
    await db.initialize()
    cfg = config()
    orch = SimpleNamespace(
        db=db,
        config=cfg,
        conversation_outbox=RecordingOutbox(),
        _discord_bot=SimpleNamespace(
            _cutover_report=SimpleNamespace(status="complete"),
            user=SimpleNamespace(id=BOT),
        ),
        bus=SimpleNamespace(emit=AsyncMock()),
    )
    handler = CommandHandler(orch, cfg)
    handler._clock = lambda: NOW
    router = DiscordInboundRouter(
        bot=orch._discord_bot,
        handler=handler,
        config=cfg,
        escalation_intake=DiscordEscalationIntake(handler, cfg),
        diagnostics=IgnoreCounter(),
        cutover_status=lambda: "complete",
        outbox_bound=lambda: True,
    )
    yield db, cfg, router
    await db.close()


async def test_live_and_history_overlap_and_edited_accepted_message_preserve_input(real_ingress):
    db, cfg, router = real_ingress
    row = message(1)
    row.guild = SimpleNamespace(id=int(cfg.discord.guild_id))
    row.channel = SimpleNamespace(id=int(CHANNEL), parent_id=None)
    row.author = SimpleNamespace(id=int(cfg.discord.authorized_users[0]), bot=False)
    row.content = f"<@{BOT}> original instruction"
    row.mentions = [SimpleNamespace(id=BOT)]
    row.webhook_id = None
    assert await router.route(row, bot_user_id=BOT) == "posted:created"
    original = await db.find_conversation_input_by_external(
        transport="discord",
        external_message_id=str(row.id),
    )
    transport = History({CHANNEL: [row]})
    backfill = ConversationBackfill(
        db=db,
        router=router,
        transport=transport,
        config=cfg,
        clock=lambda: NOW,
    )
    assert (await backfill.run(bot_user_id=BOT))["messages"] == 1
    row.content = f"<@{BOT}> edited instruction"
    # Simulate an overlapping recovery pass that started before the saved cursor.
    await db.advance_backfill_cursor(
        transport="discord",
        channel_id=CHANNEL,
        last_external_message_id=str(row.id - 1),
        now=NOW,
    )
    assert (await backfill.run(bot_user_id=BOT))["messages"] == 1
    assert (
        await db.find_conversation_input_by_external(
            transport="discord",
            external_message_id=str(row.id),
        )
        == original
    )
    assert len(await db.list_conversation_inputs(original["conversation_id"])) == 1
    assert len(await db.get_pending_messages("session", "supervisor-global")) == 1
