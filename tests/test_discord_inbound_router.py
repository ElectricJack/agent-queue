"""Exclusive escalation routing and opt-in gateway conversations."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.commands.principal import PrincipalKind, current_principal
from src.config import AppConfig, DiscordConversationConfig
from src.database import Database
from src.discord.bot import AgentQueueBot
from src.discord.escalation_intake import DiscordEscalationIntake, IntakeOutcome
from src.discord.inbound import DiscordInboundRouter
from src.discord.intake_diagnostics import IgnoreCounter
from src.models import Project
from tests.db_fixtures import lease_dsn
from tests.test_escalation_intake import (
    BOT_USER_ID,
    CHANNEL,
    HUMAN,
    THREAD,
    make_discord_config,
    make_incident,
    post_incident,
)

GUILD = "121212121212121212"
MESSAGE = "232323232323232323"
RECEIVED_AT = datetime(2026, 9, 24, tzinfo=UTC)


class RecordingHandler:
    def __init__(self, db):
        self.db = db
        self.calls = []
        self.result = {
            "success": True,
            "created": True,
            "conversation_id": "conv-1",
            "input_id": "cinput-1",
        }

    async def execute(self, command, args):
        self.calls.append((command, args, current_principal()))
        return self.result


def message(*, thread=None, mention=True, **overrides):
    values = {
        "id": int(MESSAGE),
        "guild": SimpleNamespace(id=int(GUILD)),
        "channel": SimpleNamespace(
            id=thread or int(CHANNEL), parent_id=int(CHANNEL) if thread else None
        ),
        "author": SimpleNamespace(id=int(HUMAN), bot=False),
        "content": f"<@{BOT_USER_ID}> hello" if mention else "hello",
        "mentions": [SimpleNamespace(id=BOT_USER_ID)] if mention else [],
        "webhook_id": None,
        "created_at": RECEIVED_AT,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def router(handler, *, config=None, intake=None, cutover="complete", outbox=True):
    if config is None:
        config = AppConfig(discord=make_discord_config())
        config.discord.guild_id = GUILD
        config.discord.conversation = DiscordConversationConfig(enabled=True)
        config.messages.enabled = True
        config.sessions.enabled = True
    diagnostics = IgnoreCounter()
    intake = intake or DiscordEscalationIntake(handler, config, on_ignore=diagnostics.record)
    return (
        DiscordInboundRouter(
            bot=SimpleNamespace(),
            handler=handler,
            config=config,
            escalation_intake=intake,
            diagnostics=diagnostics,
            cutover_status=lambda: cutover,
            outbox_bound=lambda: outbox,
        ),
        config,
        diagnostics,
    )


@pytest.fixture
async def db():
    database = Database(lease_dsn("discord-inbound-router"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project P"))
    yield database
    await database.close()


async def test_mention_posts_only_with_gateway_principal_and_observed_envelope(caplog):
    handler = RecordingHandler(SimpleNamespace())
    inbound, _, diagnostics = router(handler)
    caplog.set_level("INFO")
    previous_principal = current_principal()

    assert await inbound.route(message(), bot_user_id=BOT_USER_ID) == "posted:created"

    assert len(handler.calls) == 1
    command, args, principal = handler.calls[0]
    assert command == "supervisor_inbox_post"
    assert principal.kind == PrincipalKind.SERVICE
    assert principal.service_name == "discord-gateway"
    assert args == {
        "envelope": {
            "transport": "discord",
            "guild_id": GUILD,
            "channel_id": CHANNEL,
            "external_message_id": MESSAGE,
            "external_root_message_id": MESSAGE,
            "external_thread_id": None,
            "author_id": HUMAN,
            "text": "hello",
            "received_at": RECEIVED_AT.timestamp(),
            "mentions_bot": True,
        },
        "conversation_id": None,
        "source": "gateway",
    }
    assert current_principal() is previous_principal
    assert diagnostics.snapshot()["total"] == 0
    assert "hello" not in caplog.text
    assert "discord intake ignored" not in caplog.text


@pytest.mark.parametrize("precondition", ["disabled", "allowlist", "cutover", "outbox"])
async def test_unmet_preconditions_never_post(precondition):
    handler = RecordingHandler(SimpleNamespace())
    inbound, config, _ = router(
        handler,
        cutover="failed" if precondition == "cutover" else "complete",
        outbox=precondition != "outbox",
    )
    if precondition == "disabled":
        config.discord.conversation.enabled = False
    if precondition == "allowlist":
        config.discord.authorized_users = []

    assert await inbound.route(message(), bot_user_id=BOT_USER_ID) == "ignored:preconditions_unmet"
    assert handler.calls == []


async def test_escalation_is_consumed_exclusively(db):
    incident = await make_incident(db)
    _, _, thread = await post_incident(db, incident)
    handler = RecordingHandler(db)
    inbound, _, diagnostics = router(handler)

    assert await inbound.route(message(thread=thread), bot_user_id=BOT_USER_ID) == "escalation"
    assert [call[0] for call in handler.calls] == ["escalation_reply"]
    assert diagnostics.snapshot()["total"] == 0


@pytest.mark.parametrize("failure", ["classify", "persist", "refuse"])
async def test_escalation_errors_never_fall_through(db, failure):
    incident = await make_incident(db)
    _, _, thread = await post_incident(db, incident)
    handler = RecordingHandler(db)
    inbound, config, diagnostics = router(handler)
    intake = DiscordEscalationIntake(handler, config, on_ignore=diagnostics.record)
    inbound._escalation_intake = intake
    if failure == "classify":
        intake.classify = AsyncMock(side_effect=RuntimeError("unavailable"))
    elif failure == "persist":
        handler.execute = AsyncMock(side_effect=RuntimeError("unavailable"))
    else:
        handler.result = {"success": False, "error": "refused"}

    expected = "escalation_failed" if failure == "classify" else "escalation"
    assert await inbound.route(message(thread=thread), bot_user_id=BOT_USER_ID) == expected
    assert all(call[0] != "supervisor_inbox_post" for call in handler.calls)
    if failure == "persist":
        assert handler.execute.await_args.args[0] == "escalation_reply"
    if failure == "classify":
        assert diagnostics.snapshot()["ignored"] == {"classify_error": 1}


async def test_disabled_escalation_intake_still_owns_bound_threads(db):
    incident = await make_incident(db)
    _, _, thread = await post_incident(db, incident)
    handler = RecordingHandler(db)
    inbound, config, diagnostics = router(handler)
    config.discord.escalation.enabled = False
    db.find_conversation_by_thread = AsyncMock()

    assert await inbound.route(message(thread=thread), bot_user_id=BOT_USER_ID) == (
        "ignored:escalation_thread"
    )
    db.find_conversation_by_thread.assert_not_awaited()
    assert handler.calls == []
    assert diagnostics.snapshot()["ignored"] == {"escalation_thread": 1}


async def test_follow_up_uses_the_durable_conversation_root_and_id(db):
    accepted = await db.accept_conversation_input(
        transport="discord",
        guild_id=GUILD,
        channel_id=CHANNEL,
        external_message_id="root",
        external_root_message_id="root",
        external_thread_id=None,
        author_id=HUMAN,
        verified_actor=f"human:discord:{HUMAN}",
        text="question",
        audience=[HUMAN],
        source="gateway",
        received_at=1000.0,
        conversation_id=None,
        brief="question",
        now=1000.0,
    )
    conversation_id = accepted["conversation"]["id"]
    await db.bind_conversation_thread(conversation_id, external_thread_id=THREAD, now=1001.0)
    handler = RecordingHandler(db)
    inbound, _, _ = router(handler)

    assert (
        await inbound.route(
            message(thread=THREAD, mention=False), bot_user_id=BOT_USER_ID, source="backfill"
        )
        == "posted:created"
    )
    _, args, principal = handler.calls[0]
    assert args["conversation_id"] == conversation_id
    assert args["envelope"]["external_root_message_id"] == "root"
    assert args["envelope"]["external_thread_id"] == THREAD
    assert args["source"] == "backfill"
    assert principal.service_name == "discord-gateway"


async def test_unknown_thread_costs_only_the_two_binding_lookups():
    database = SimpleNamespace(
        find_escalation_by_thread=AsyncMock(return_value=None),
        find_conversation_by_thread=AsyncMock(return_value=None),
    )
    handler = RecordingHandler(database)
    inbound, _, diagnostics = router(handler)

    assert await inbound.route(message(thread=THREAD), bot_user_id=BOT_USER_ID) == (
        "ignored:unknown_thread"
    )
    database.find_escalation_by_thread.assert_awaited_once_with(
        channel_id=CHANNEL, thread_id=THREAD
    )
    database.find_conversation_by_thread.assert_awaited_once_with(
        transport="discord", channel_id=CHANNEL, external_thread_id=THREAD
    )
    assert diagnostics.snapshot()["ignored"] == {"unknown_thread": 1}
    assert handler.calls == []


async def test_chatter_costs_no_database_lookup_and_one_final_diagnostic(caplog):
    handler = RecordingHandler(SimpleNamespace())
    inbound, _, diagnostics = router(handler)
    caplog.set_level("INFO")

    assert await inbound.route(message(mention=False), bot_user_id=BOT_USER_ID) == (
        "ignored:no_bot_mention"
    )
    assert handler.calls == []
    assert diagnostics.snapshot()["ignored"] == {"no_bot_mention": 1}
    ignored = [record.getMessage() for record in caplog.records if "intake ignored" in record.msg]
    assert ignored == [
        f"discord intake ignored reason=no_bot_mention guild={GUILD} channel={CHANNEL} "
        f"message={MESSAGE} author={HUMAN}"
    ]


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"guild": None}, "dm"),
        ({"webhook_id": 123}, "webhook_author"),
        ({"author": SimpleNamespace(id=int(HUMAN), bot=True)}, "bot_author"),
        ({"mentions": [], "content": "@everyone hello"}, "no_bot_mention"),
        ({"guild": SimpleNamespace(id=123)}, "foreign_guild"),
        ({"author": SimpleNamespace(id=123, bot=False)}, "author_not_allowlisted"),
    ],
)
async def test_non_conversation_inputs_are_silent_and_counted(overrides, code):
    handler = RecordingHandler(SimpleNamespace())
    inbound, _, diagnostics = router(handler)

    assert await inbound.route(message(**overrides), bot_user_id=BOT_USER_ID) == f"ignored:{code}"
    assert diagnostics.snapshot()["ignored"] == {code: 1}
    assert handler.calls == []


@pytest.mark.parametrize("failed", [False, True])
async def test_detailed_outcome_stops_before_observing_conversations(failed):
    intake = SimpleNamespace(
        handle_detailed=AsyncMock(return_value=IntakeOutcome(not failed, failed, None))
    )
    inbound, _, _ = router(RecordingHandler(SimpleNamespace()), intake=intake)
    # Deliberately lacks gateway fields: a stop must not try observing it.
    expected = "escalation_failed" if failed else "escalation"
    assert await inbound.route(object(), bot_user_id=BOT_USER_ID) == expected


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ({"success": True, "created": False}, "posted:replayed"),
        ({"success": False, "error_code": "oversize", "error": "private text"}, "refused:oversize"),
    ],
)
async def test_post_results_return_stable_codes_without_logging_content(result, expected, caplog):
    handler = RecordingHandler(SimpleNamespace())
    handler.result = result
    inbound, _, _ = router(handler)
    caplog.set_level("INFO")

    assert await inbound.route(message(), bot_user_id=BOT_USER_ID) == expected
    assert "private text" not in caplog.text


async def test_lookup_failure_does_not_post_or_raise():
    database = SimpleNamespace(find_escalation_by_thread=AsyncMock(side_effect=RuntimeError()))
    handler = RecordingHandler(database)
    inbound, _, _ = router(handler)
    assert (
        await inbound.route(message(thread=THREAD), bot_user_id=BOT_USER_ID) == "escalation_failed"
    )
    assert handler.calls == []


async def test_conversation_lookup_failure_stays_ignored_and_counted():
    database = SimpleNamespace(
        find_escalation_by_thread=AsyncMock(return_value=None),
        find_conversation_by_thread=AsyncMock(side_effect=RuntimeError("unavailable")),
    )
    handler = RecordingHandler(database)
    inbound, _, diagnostics = router(handler)

    assert await inbound.route(message(thread=THREAD), bot_user_id=BOT_USER_ID) == (
        "ignored:classify_error"
    )
    assert handler.calls == []
    assert diagnostics.snapshot()["ignored"] == {"classify_error": 1}


async def test_failed_post_restores_principal_and_stops_without_a_transport_call():
    handler = RecordingHandler(SimpleNamespace())
    handler.execute = AsyncMock(side_effect=RuntimeError("unavailable"))
    inbound, _, _ = router(handler)
    previous_principal = current_principal()

    assert await inbound.route(message(), bot_user_id=BOT_USER_ID) == "refused:post_failed"
    assert current_principal() is previous_principal
    handler.execute.assert_awaited_once()


async def test_refused_bound_escalation_text_stays_consumed(db):
    incident = await make_incident(db)
    _, _, thread = await post_incident(db, incident)
    handler = RecordingHandler(db)
    inbound, _, diagnostics = router(handler)

    assert (
        await inbound.route(message(thread=thread, content=" "), bot_user_id=BOT_USER_ID)
        == "escalation"
    )
    assert handler.calls == []
    assert diagnostics.snapshot()["ignored"] == {"empty_text": 1}


async def test_bot_delegation_observes_live_outbox_binding_and_rebuilds_for_new_handler():
    handler = RecordingHandler(SimpleNamespace())
    inbound, config, _ = router(handler)
    bot = AgentQueueBot.__new__(AgentQueueBot)
    bot.config = config
    bot.orchestrator = SimpleNamespace(_command_handler=handler)
    bot._escalation_intake_impl = None
    bot._intake_diagnostics = IgnoreCounter()
    bot._cutover_report = SimpleNamespace(status="complete")
    bot._connection = SimpleNamespace(user=SimpleNamespace(id=BOT_USER_ID))

    await bot.on_message(message())
    assert handler.calls == []
    bot.orchestrator.conversation_outbox = SimpleNamespace(bound=True)
    first = bot._inbound_router()
    await bot.on_message(message())
    assert [call[0] for call in handler.calls] == ["supervisor_inbox_post"]
    assert bot._inbound_router() is first

    replacement = RecordingHandler(SimpleNamespace())
    bot.orchestrator._command_handler = replacement
    assert bot._inbound_router() is not first
    await bot.on_message(message())
    assert [call[0] for call in replacement.calls] == ["supervisor_inbox_post"]


def test_edits_have_no_gateway_handler():
    assert not hasattr(AgentQueueBot, "on_message_edit")
