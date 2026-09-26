"""Verified internal intake, durable quotas and replay-safe conversation notifications."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select, update

from src.commands.handler import CommandHandler
from src.commands.principal import (
    TRUSTED_LOCAL,
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.config import AppConfig
from src.conversations.outbox import RecordingOutbox
from src.database import Database
from src.database.tables import conversation_inputs, messages, supervisor_conversations
from src.profiles.capabilities import DENY_ALL
from tests.db_fixtures import lease_dsn

GUILD = "111111111111111111"
CHANNEL = "222222222222222222"
AUTHOR = "333333333333333333"
BOT = "444444444444444444"
THREAD = "555555555555555555"
NOW = 10000.0
GATEWAY = ExecutionPrincipal.service("discord-gateway")


@pytest.fixture
async def env():
    db = Database(lease_dsn("supervisor-inbox-commands"))
    await db.initialize()
    config = AppConfig()
    config.discord.conversation.enabled = True
    config.discord.guild_id = GUILD
    config.discord.channel_id = CHANNEL
    config.discord.authorized_users = [AUTHOR]
    config.messages.enabled = True
    config.sessions.enabled = True
    orch = SimpleNamespace(
        db=db,
        config=config,
        conversation_outbox=RecordingOutbox(),
        _discord_bot=SimpleNamespace(
            _cutover_report=SimpleNamespace(status="complete"), user=SimpleNamespace(id=BOT)
        ),
        bus=SimpleNamespace(emit=AsyncMock()),
    )
    handler = CommandHandler(orch, config)
    handler._clock = lambda: NOW
    yield handler, db
    await db.close()


def args(index=0, **overrides):
    external_id = str(900000000000000000 + index)
    value = {
        "envelope": {
            "transport": "discord",
            "guild_id": GUILD,
            "channel_id": CHANNEL,
            "external_message_id": external_id,
            "external_root_message_id": external_id,
            "external_thread_id": None,
            "author_id": AUTHOR,
            "text": f"<@{BOT}> hello supervisor",
            "received_at": NOW,
            "mentions_bot": True,
        },
        "source": "gateway",
    }
    value.update(overrides)
    return value


async def post(handler, values=None, principal=GATEWAY):
    with principal_context(principal):
        return await handler.execute("supervisor_inbox_post", values or args())


async def counts(db):
    async with db._engine.connect() as conn:
        return tuple(
            [
                await conn.scalar(select(func.count()).select_from(table))
                for table in (supervisor_conversations, conversation_inputs, messages)
            ]
        )


async def test_gateway_atomic_rows_stable_brief_outbox_and_event(env):
    handler, db = env
    outbox = handler.orchestrator.conversation_outbox
    enqueue = outbox.enqueue

    async def assert_persisted(**values):
        assert await counts(db) == (1, 1, 1)
        return await enqueue(**values)

    outbox.enqueue = assert_persisted
    result = await post(handler)
    assert result == {
        "success": True,
        "created": True,
        "conversation_id": result["conversation_id"],
        "input_id": result["input_id"],
        "supervisor_message_id": f"msg-{result['input_id']}",
        "state": "accepted",
    }
    item = await db.get_conversation_input(result["input_id"])
    assert item["verified_actor"] == f"human:discord:{AUTHOR}"
    assert item["text"] == "hello supervisor" and item["source"] == "gateway"
    notice = await db.get_message(result["supervisor_message_id"])
    assert (
        notice.project_id,
        notice.to_kind,
        notice.to_id,
        notice.body_kind,
        notice.thread_id,
        notice.delivered_at,
    ) == (
        None,
        "session",
        "supervisor-global",
        "conversation_input",
        f"conversation:{result['conversation_id']}",
        None,
    )
    assert result["conversation_id"] in notice.body and result["input_id"] in notice.body
    assert "aq supervisor-inbox reply" in notice.body and "dashboard action" in notice.body
    assert outbox.rows == [
        {
            "id": "out-1",
            "owner_id": result["conversation_id"],
            "kind": "thread_open",
            "dedup_key": "conv-open:discord:900000000000000000",
            "payload": {
                "channel_id": CHANNEL,
                "root_message_id": "900000000000000000",
                "conversation_id": result["conversation_id"],
            },
            "priority": 20,
            "due_at": None,
        }
    ]
    handler.orchestrator.bus.emit.assert_any_await(
        "conversation.input_received.v1",
        {
            "conversation_id": result["conversation_id"],
            "input_id": result["input_id"],
            "transport": "discord",
            "verified_actor": f"human:discord:{AUTHOR}",
            "created": True,
            "source": "gateway",
        },
    )


@pytest.mark.parametrize(
    "principal",
    [
        TRUSTED_LOCAL,
        ExecutionPrincipal.service("discord:111"),
        ExecutionPrincipal.service("gateway"),
        ExecutionPrincipal(kind=PrincipalKind.SESSION, policy=DENY_ALL, elevated=True),
        ExecutionPrincipal(kind=PrincipalKind.PLAYBOOK, policy=DENY_ALL),
    ],
)
async def test_only_exact_gateway_or_local_provenance_is_admitted(env, principal):
    handler, db = env
    # Call the handler under the principal as well: capability policy cannot
    # substitute for the handler's load-bearing kind/name check.
    with principal_context(principal):
        result = await handler._cmd_supervisor_inbox_post(args())
    assert result["error_code"] == "out_of_scope"
    assert await counts(db) == (0, 0, 0)


async def test_local_test_provenance_and_backfill_source(env):
    handler, db = env
    result = await post(handler, args(provenance="test"), TRUSTED_LOCAL)
    assert result["success"] is True
    assert (await db.get_conversation_input(result["input_id"]))["source"] == "test"
    result = await post(handler, args(1, source="backfill"))
    assert (await db.get_conversation_input(result["input_id"]))["source"] == "backfill"


@pytest.mark.parametrize(
    "field",
    [
        "actor",
        "actor_id",
        "human",
        "verified_actor",
        "from_kind",
        "from_id",
        "to_kind",
        "to_id",
        "session",
        "session_id",
        "destination",
        "thread_id",
        "supervisor_owner",
    ],
)
async def test_authority_args_are_rejected(env, field):
    handler, db = env
    result = await post(handler, args(**{field: "forged"}))
    assert result["error_code"] == "spoofed_identity"
    assert await counts(db) == (0, 0, 0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("author_id", "1"),
        ("guild_id", "invalid"),
        ("received_at", float("nan")),
        ("mentions_bot", "yes"),
        ("unexpected", "field"),
    ],
)
async def test_invalid_envelope_is_refused(env, field, value):
    handler, db = env
    values = args()
    values["envelope"][field] = value
    result = await post(handler, values)
    assert result["error_code"] == "invalid_envelope"
    assert await counts(db) == (0, 0, 0)


@pytest.mark.parametrize(
    "mode,code",
    [
        ("disabled", "conversation_disabled"),
        ("empty", "empty_allowlist"),
        ("messages", "messages_disabled"),
        ("sessions", "sessions_disabled"),
        ("cutover", "cutover_incomplete"),
        ("outbox", "outbox_unbound"),
    ],
)
async def test_preconditions_are_revalidated(env, mode, code):
    handler, db = env
    if mode == "disabled":
        handler.config.discord.conversation.enabled = False
    elif mode == "empty":
        handler.config.discord.authorized_users = []
    elif mode in {"messages", "sessions"}:
        getattr(handler.config, mode).enabled = False
    elif mode == "cutover":
        handler.orchestrator._discord_bot._cutover_report.status = "pending"
    else:
        handler.orchestrator.conversation_outbox.bound = False
    result = await post(handler)
    assert result["error_code"] == "preconditions_unmet" and code in result["unmet"]
    assert await counts(db) == (0, 0, 0)


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("author_id", "666666666666666666", "author_not_allowlisted"),
        ("guild_id", "666666666666666666", "foreign_destination"),
        ("channel_id", "666666666666666666", "foreign_destination"),
        ("text", f"<@{BOT}>\x00 ", "empty_text"),
        ("mentions_bot", False, "invalid_envelope"),
    ],
)
async def test_author_destination_and_mention_checks(env, field, value, code):
    handler, db = env
    values = args()
    values["envelope"][field] = value
    result = await post(handler, values)
    assert result["error_code"] == code
    assert await counts(db) == (0, 0, 0)


async def test_oversize_is_unicode_bounded_and_notice_deduped(env):
    handler, db = env
    values = args()
    values["envelope"]["text"] = "é" * 4001
    for _ in range(2):
        assert (await post(handler, values))["error_code"] == "oversize"
    assert await counts(db) == (0, 0, 0)
    rows = handler.orchestrator.conversation_outbox.rows
    assert len(rows) == 1 and rows[0]["payload"]["char_count"] == 4001
    assert rows[0]["dedup_key"] == "conv-notice:oversize:discord:900000000000000000"


async def test_replay_spends_no_quota_and_limit_survives_new_handler(env):
    handler, db = env
    first = await post(handler)
    for index in range(1, 10):
        assert (await post(handler, args(index)))["success"] is True
    replay = await post(handler)
    assert replay == {**first, "created": False}
    assert await db.count_conversation_inputs(since=NOW - 600, author_id=AUTHOR) == 10
    assert len(handler.orchestrator.conversation_outbox.rows) == 10
    fresh = CommandHandler(handler.orchestrator, handler.config)
    fresh._clock = handler._clock
    for index in (10, 11):
        result = await post(fresh, args(index))
        assert result["error_code"] == "rate_limited" and result["scope"] == "author"
    notices = [r for r in handler.orchestrator.conversation_outbox.rows if r["kind"] == "notice"]
    assert len(notices) == 1
    assert notices[0]["dedup_key"] == f"conv-notice:ratelimit:discord:author:{AUTHOR}:9600"
    fresh._clock = lambda: NOW + 601
    assert (await post(fresh, args(12)))["success"] is True


async def test_concurrent_backfill_accepts_obey_author_limit(env):
    handler, db = env
    values = [args(index, source="backfill") for index in range(12)]
    for value in values:
        value["envelope"]["received_at"] = NOW - 86400
    results = await asyncio.gather(*(post(handler, value) for value in values))
    assert sum(r.get("success", False) for r in results) == 10
    assert [r["error_code"] for r in results if not r["success"]] == ["rate_limited"] * 2
    assert await counts(db) == (10, 10, 10)


async def test_channel_limit_across_authors(env):
    handler, _ = env
    authors = [str(700000000000000000 + index) for index in range(7)]
    handler.config.discord.authorized_users = authors
    for index in range(60):
        value = args(index)
        value["envelope"]["author_id"] = authors[index // 10]
        assert (await post(handler, value))["success"] is True
    value = args(60)
    value["envelope"]["author_id"] = authors[-1]
    result = await post(handler, value)
    assert result["error_code"] == "rate_limited" and result["scope"] == "channel"


async def follow_up(handler, db, first):
    await db.bind_conversation_thread(first["conversation_id"], external_thread_id=THREAD, now=NOW)
    value = args(1, conversation_id=first["conversation_id"])
    value["envelope"].update(
        external_thread_id=THREAD,
        mentions_bot=False,
        external_root_message_id=args()["envelope"]["external_message_id"],
    )
    return value


async def test_follow_up_checks_binding_and_audience_then_closed_notice(env):
    handler, db = env
    first = await post(handler)
    value = await follow_up(handler, db, first)
    accepted = await post(handler, value)
    assert accepted["success"] is True and accepted["conversation_id"] == first["conversation_id"]
    assert len(handler.orchestrator.conversation_outbox.rows) == 1
    await db.set_conversation_state(first["conversation_id"], state="closed", now=NOW)
    value["envelope"]["external_message_id"] = str(900000000000000002)
    for _ in range(2):
        assert (await post(handler, value))["error_code"] == "conversation_closed"
    assert len(handler.orchestrator.conversation_outbox.rows) == 2
    assert handler.orchestrator.conversation_outbox.rows[-1]["dedup_key"] == (
        f"conv-notice:closed:{first['conversation_id']}"
    )


async def test_follow_up_cannot_forge_conversation_thread_or_expand_audience(env):
    handler, db = env
    first = await post(handler)
    value = await follow_up(handler, db, first)
    value["envelope"]["external_thread_id"] = "888888888888888888"
    assert (await post(handler, value))["error_code"] == "foreign_destination"
    value["envelope"]["external_thread_id"] = THREAD
    other = "666666666666666666"
    handler.config.discord.authorized_users.append(other)
    value["envelope"]["author_id"] = other
    assert (await post(handler, value))["error_code"] == "author_not_allowlisted"
    assert await counts(db) == (1, 1, 1)


async def test_replay_repairs_thread_open_enqueue_failure(env):
    handler, db = env
    outbox = handler.orchestrator.conversation_outbox
    enqueue = outbox.enqueue
    outbox.enqueue = AsyncMock(side_effect=RuntimeError("temporary outbox failure"))
    failed = await post(handler)
    assert "temporary outbox failure" in failed["error"]
    assert await counts(db) == (1, 1, 1) and outbox.rows == []
    outbox.enqueue = enqueue
    replay = await post(handler)
    assert replay["success"] is True and replay["created"] is False
    assert await counts(db) == (1, 1, 1) and len(outbox.rows) == 1


async def test_ignored_content_is_absent_from_command_logs(env, caplog):
    handler, _ = env
    value = args()
    value["envelope"]["text"] = "private rejected text"
    value["envelope"]["channel_id"] = "888888888888888888"
    with caplog.at_level("DEBUG", logger="src.commands.handler"):
        assert (await post(handler, value))["error_code"] == "foreign_destination"
    assert "private rejected text" not in caplog.text


async def test_tombstone_replay_never_recreates_work(env):
    handler, db = env
    first = await post(handler)
    assert await db.expire_conversation_text(older_than=NOW + 1, now=NOW + 2) == 1
    replay = await post(handler)
    assert replay == {**first, "created": False, "state": "expired"}
    assert await counts(db) == (1, 1, 1)
    assert len(handler.orchestrator.conversation_outbox.rows) == 1


def reply_args(first, **overrides):
    return {
        "conversation_id": first["conversation_id"],
        "input_id": first["input_id"],
        "text": "Answer from the supervisor",
        "idempotency_key": "answer-1",
        **overrides,
    }


async def live_supervisor(db, **overrides):
    from src.models import SessionRecord
    from src.profiles.capabilities import CapabilityPolicy

    await db.create_session(
        SessionRecord(
            id="global-launch",
            project_id=None,
            profile_id="supervisor",
            harness="codex",
            provider="openai",
            name="supervisor-global",
            lifecycle="named",
            work_dir="/tmp",
            epoch="epoch",
            instance_token="live-token",
            started_at=NOW,
            state="running",
        )
    )
    return ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(aq_commands=["supervisor_inbox_reply"]),
        session_id="global-launch",
        session_instance_token="live-token",
        elevated=True,
        **overrides,
    )


async def reply(handler, values, principal=TRUSTED_LOCAL):
    with principal_context(principal):
        return await handler.execute("supervisor_inbox_reply", values)


async def test_reply_live_global_supervisor_is_durable_and_idempotent(env):
    handler, db = env
    first = await post(handler)
    principal = await live_supervisor(db)
    result = await reply(handler, reply_args(first), principal)
    assert result == {
        "success": True,
        "created": True,
        "reply_message_id": result["reply_message_id"],
        "delivery_dedup_key": f"conv-reply:{result['reply_message_id']}",
        "discord_text_chars": result["discord_text_chars"],
        "truncated": False,
    }
    msg = await db.get_message(result["reply_message_id"])
    assert (
        msg.from_kind,
        msg.from_id,
        msg.to_kind,
        msg.to_id,
        msg.thread_id,
        msg.reply_to_id,
        msg.body_kind,
        msg.body,
    ) == (
        "session",
        "supervisor-global",
        "user",
        f"discord:{AUTHOR}",
        f"conversation:{first['conversation_id']}",
        first["supervisor_message_id"],
        "conversation_reply",
        "Answer from the supervisor",
    )
    assert (await db.get_conversation_input(first["input_id"]))["state"] == "answered"
    rows = handler.orchestrator.conversation_outbox.rows
    assert rows[-1]["payload"] == {
        "conversation_id": first["conversation_id"],
        "input_id": first["input_id"],
        "reply_message_id": result["reply_message_id"],
        "text": f"Answer from the supervisor (aq-conv:{result['delivery_dedup_key']})",
    }
    assert result["discord_text_chars"] == len(rows[-1]["payload"]["text"])
    # The first durable text wins even when a retry carries a changed body.
    replay = await reply(handler, reply_args(first, text="changed"), principal)
    assert replay == {**result, "created": False}
    assert len(rows) == 2
    handler.orchestrator.bus.emit.assert_any_await(
        "conversation.reply_queued.v1",
        {
            "conversation_id": first["conversation_id"],
            "input_id": first["input_id"],
            "reply_message_id": result["reply_message_id"],
            "delivery_dedup_key": result["delivery_dedup_key"],
            "created": True,
        },
    )


@pytest.mark.parametrize(
    "mode", ["stale", "service", "project", "plain", "stopped", "absent", "playbook"]
)
async def test_reply_requires_live_global_supervisor_launch(env, mode):
    from dataclasses import replace

    handler, db = env
    first = await post(handler)
    principal = await live_supervisor(db)
    if mode == "stale":
        principal = replace(principal, session_instance_token="old-token")
    elif mode == "service":
        principal = GATEWAY
    elif mode == "project":
        principal = replace(principal, project_id="some-project")
    elif mode == "plain":
        principal = replace(principal, elevated=False)
    elif mode == "playbook":
        principal = replace(principal, kind=PrincipalKind.PLAYBOOK)
    elif mode == "stopped":
        await db.update_session("global-launch", state="stopped")
    elif mode == "absent":
        await db.update_session("global-launch", name="other")
    with principal_context(principal):
        result = await handler._cmd_supervisor_inbox_reply(reply_args(first))
    assert result["error_code"] == "out_of_scope"
    assert len(handler.orchestrator.conversation_outbox.rows) == 1
    assert (await db.get_conversation_input(first["input_id"]))["state"] == "accepted"


@pytest.mark.parametrize(
    "field",
    [
        "actor",
        "actor_id",
        "human",
        "verified_actor",
        "from_kind",
        "from_id",
        "to_kind",
        "to_id",
        "session",
        "session_id",
        "destination",
        "thread_id",
        "supervisor_owner",
    ],
)
async def test_reply_rejects_spoofed_authority(env, field):
    handler, _ = env
    first = await post(handler)
    assert (await reply(handler, reply_args(first, **{field: "forged"})))[
        "error_code"
    ] == "spoofed_identity"


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", ""),
        ("text", " " * 10),
        ("text", "x" * 16001),
        ("text", 1),
        ("idempotency_key", ""),
        ("idempotency_key", "x" * 129),
        ("conversation_id", None),
        ("input_id", []),
    ],
    ids=[
        "empty-text",
        "blank-text",
        "long-text",
        "nonstring-text",
        "empty-key",
        "long-key",
        "missing-conversation",
        "nonstring-input",
    ],
)
async def test_reply_validates_request(env, field, value):
    handler, _ = env
    first = await post(handler)
    assert (await reply(handler, reply_args(first, **{field: value})))[
        "error_code"
    ] == "invalid_request"


async def test_reply_checks_input_binding_closed_and_revoked(env):
    handler, db = env
    first = await post(handler)
    other = await post(handler, args(1))
    assert (await reply(handler, reply_args(first, input_id=other["input_id"])))[
        "error_code"
    ] == "input_not_in_conversation"
    assert (await reply(handler, reply_args(first, input_id="missing")))[
        "error_code"
    ] == "input_not_in_conversation"
    assert (await reply(handler, reply_args(first, conversation_id="missing")))[
        "error_code"
    ] == "conversation_not_found"
    await db.set_conversation_state(first["conversation_id"], state="closed", now=NOW)
    assert (await reply(handler, reply_args(first)))["error_code"] == "conversation_closed"
    async with db._engine.begin() as conn:
        await conn.execute(
            update(conversation_inputs)
            .where(conversation_inputs.c.id == other["input_id"])
            .values(state="revoked")
        )
    assert (await reply(handler, reply_args(other)))["error_code"] == "input_revoked"


async def test_reply_enqueue_recovery_uses_original_full_text(env):
    handler, db = env
    first = await post(handler)
    outbox = handler.orchestrator.conversation_outbox
    enqueue = outbox.enqueue
    outbox.enqueue = AsyncMock(side_effect=RuntimeError("temporary outbox failure"))
    original = "é" * 5000
    failed = await reply(handler, reply_args(first, text=original))
    assert "temporary outbox failure" in failed["error"]
    assert (await db.get_conversation_input(first["input_id"]))["state"] == "answered"
    outbox.enqueue = enqueue
    handler.orchestrator.dashboard_links = SimpleNamespace(
        resolve=AsyncMock(return_value=SimpleNamespace(url="https://dashboard.example"))
    )
    recovered = await reply(handler, reply_args(first, text="changed"))
    assert recovered["created"] is False and recovered["truncated"] is True
    assert recovered["discord_text_chars"] == 1900
    assert (await db.get_message(recovered["reply_message_id"])).body == original
    assert outbox.rows[-1]["payload"]["text"].startswith("é")


async def test_reply_transcript_tail_and_message_sent_never_enqueue_delivery(env):
    from src.messages.delivery import MessageDeliveryEngine

    handler, db = env
    first = await post(handler)
    await db.mark_delivered(first["supervisor_message_id"], via="nudge")
    async with db._engine.begin() as conn:
        await conn.execute(
            update(messages)
            .where(messages.c.id == first["supervisor_message_id"])
            .values(delivered_at=NOW)
        )
    sessions = SimpleNamespace(tail_assistant_turn=AsyncMock(return_value="transcript answer"))
    engine = MessageDeliveryEngine(db, sessions, handler.config, bus=handler.orchestrator.bus)
    assert await engine.check_reply_timeouts() == 1
    sessions.tail_assistant_turn.assert_awaited_once()
    assert any(
        call.args[0] == "message.sent" for call in handler.orchestrator.bus.emit.await_args_list
    )
    item = (await db.list_conversation_inputs(first["conversation_id"]))[0]
    assert item["state"] == "accepted"
    assert not any(
        r["dedup_key"].startswith("conv-reply:")
        for r in handler.orchestrator.conversation_outbox.rows
    )
    sent = await handler.execute(
        "message_send",
        {
            "to_kind": "user",
            "to_id": f"discord:{AUTHOR}",
            "body": "generic answer",
            "from_kind": "session",
            "from_id": "supervisor-global",
            "thread_id": f"conversation:{first['conversation_id']}",
        },
    )
    assert "error" not in sent
    await engine.run_delivery_pass()
    assert not any(
        r["dedup_key"].startswith("conv-reply:")
        for r in handler.orchestrator.conversation_outbox.rows
    )
    assert (await db.get_conversation_input(first["input_id"]))["state"] == "accepted"


async def test_status_reports_preconditions_limits_counts_backfill_and_intake(env):
    from src.api.models.supervisor_inbox import SupervisorInboxStatusResponse
    from src.discord.intake_diagnostics import IgnoreCounter

    handler, db = env
    first = await post(handler)
    await db.bind_conversation_thread(first["conversation_id"], external_thread_id=THREAD, now=NOW)
    await db.advance_backfill_cursor(
        transport="discord",
        channel_id=CHANNEL,
        last_external_message_id="900000000000000000",
        now=NOW,
    )
    await db.record_intake_gap(
        transport="discord",
        channel_id=THREAD,
        gap_from=NOW - 100,
        gap_to=NOW,
        reason="history_forbidden",
        now=NOW,
    )
    counter = IgnoreCounter(clock=lambda: NOW)
    counter.record("not_mention")
    bot = handler.orchestrator._discord_bot
    bot._intake_diagnostics = counter
    bot.intents = SimpleNamespace(message_content=True)
    bot._conversation_backfill = lambda: SimpleNamespace(
        diagnostics=lambda bot: {
            "message_content_intent": True,
            "permissions": {"send_messages": True},
        }
    )
    result = await handler.execute("supervisor_inbox_status", {})
    assert SupervisorInboxStatusResponse.model_validate(result).model_dump() == result
    assert result["enabled"] and result["preconditions"] == {"ok": True, "unmet": []}
    assert result["diagnostics"] == {
        "message_content_intent": True,
        "permissions": {"send_messages": True},
        "outbox_bound": True,
    }
    assert result["limits"] == {
        "max_input_chars": 4000,
        "author_window_limit": 10,
        "channel_window_limit": 60,
        "window_seconds": 600,
        "max_reply_chars": 1900,
    }
    assert result["counts"] == {
        "by_state": {"opening": 0, "open": 1, "closed": 0, "delivery_blocked": 0},
        "inputs_pending_supervisor": 1,
    }
    assert result["backfill"]["cursors"][0]["channel_id"] == CHANNEL
    assert result["backfill"]["gaps"][0]["reason"] == "history_forbidden"
    assert result["intake"] == {
        "available": True,
        "window_seconds": 3600,
        "total": 1,
        "ignored": {"not_mention": 1},
    }


async def test_status_without_bot_reports_unavailable_diagnostics(env):
    handler, _ = env
    handler.orchestrator._discord_bot = None
    handler.orchestrator.conversation_outbox = None
    handler.config.discord.conversation.enabled = False
    result = await handler.execute("supervisor_inbox_status", {})
    assert result["enabled"] is False
    assert result["preconditions"]["ok"] is False
    assert "conversation_disabled" in result["preconditions"]["unmet"]
    assert "outbox_unbound" in result["preconditions"]["unmet"]
    assert result["diagnostics"] == {
        "message_content_intent": None,
        "permissions": None,
        "outbox_bound": False,
    }
    assert result["intake"]["available"] is False


async def test_history_pages_conversations_and_filters_state(env):
    from src.api.models.supervisor_inbox import SupervisorInboxHistoryResponse

    handler, db = env
    first = await post(handler)
    handler._clock = lambda: NOW + 10
    second = await post(handler, args(1))
    handler._clock = lambda: NOW + 20
    third = await post(handler, args(2))
    page = await handler.execute("supervisor_inbox_history", {"limit": 2})
    assert SupervisorInboxHistoryResponse.model_validate(page).model_dump() == page
    assert [c["id"] for c in page["conversations"]] == [
        third["conversation_id"],
        second["conversation_id"],
    ]
    assert page["next_before"] == NOW + 10
    tail = await handler.execute(
        "supervisor_inbox_history", {"limit": 2, "before": page["next_before"]}
    )
    assert [c["id"] for c in tail["conversations"]] == [first["conversation_id"]]
    assert tail["next_before"] is None
    await db.set_conversation_state(first["conversation_id"], state="closed", now=NOW + 30)
    filtered = await handler.execute("supervisor_inbox_history", {"states": ["closed"]})
    assert [c["id"] for c in filtered["conversations"]] == [first["conversation_id"]]


async def test_history_pages_inputs_and_redacts_expired_text(env):
    handler, db = env
    first = await post(handler)
    follow = await follow_up(handler, db, first)
    follow["envelope"]["received_at"] = NOW + 10
    handler._clock = lambda: NOW + 10
    second = await post(handler, follow)
    answer = await reply(handler, reply_args(second))
    query = {"conversation_id": first["conversation_id"], "limit": 1}
    page = await handler.execute("supervisor_inbox_history", query)
    conversation = page["conversations"][0]
    item = conversation["inputs"][0]
    assert item["id"] == second["input_id"] and item["text_expired"] is False
    assert item["reply_message_id"] == answer["reply_message_id"]
    assert item["reply_body"] == "Answer from the supervisor"
    assert page["next_before"] == conversation["next_before"] == NOW + 10
    await db.expire_conversation_text(older_than=NOW + 1, now=NOW + 20)
    tail = await handler.execute(
        "supervisor_inbox_history", {**query, "before": page["next_before"]}
    )
    expired = tail["conversations"][0]["inputs"][0]
    assert expired["id"] == first["input_id"]
    assert expired["text"] is None and expired["text_expired"] is True
    assert tail["next_before"] is None
    assert (await handler.execute("supervisor_inbox_history", {"conversation_id": "missing"}))[
        "error_code"
    ] == "conversation_not_found"


@pytest.mark.parametrize("command", ["supervisor_inbox_status", "supervisor_inbox_history"])
@pytest.mark.parametrize("mode", ["project", "worker", "service", "playbook", "global"])
async def test_status_history_scope_is_enforced_on_direct_calls(env, command, mode):
    from src.profiles.capabilities import CapabilityPolicy

    handler, _ = env
    principal = ExecutionPrincipal(
        kind={"service": PrincipalKind.SERVICE, "playbook": PrincipalKind.PLAYBOOK}.get(
            mode, PrincipalKind.SESSION
        ),
        policy=CapabilityPolicy.from_namespaces(aq_commands=[command]),
        elevated=mode in {"global", "project"},
        project_id="project" if mode == "project" else None,
    )
    with principal_context(principal):
        result = await handler.execute(command, {})
    if mode == "global":
        assert result["success"] is True
    else:
        assert result["error_code"] == "out_of_scope"


@pytest.mark.parametrize(
    "query",
    [
        {"limit": 0},
        {"limit": 101},
        {"limit": True},
        {"states": "open"},
        {"states": ["invalid"]},
        {"before": float("nan")},
        {"before": float("inf")},
        {"conversation_id": ""},
        {"project_id": "project"},
    ],
)
async def test_history_rejects_invalid_filters(env, query):
    handler, _ = env
    assert (await handler.execute("supervisor_inbox_history", query))[
        "error_code"
    ] == "invalid_request"
