"""One verified input across gateway, backfill and legacy thread replay."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from src.asks.intake import clear_intent
from src.commands.principal import ExecutionPrincipal, principal_context
from src.database.tables import ask_decisions, ask_reconciliation_cases, conversation_inputs
from tests.test_ask_discussion import ask_for
from tests.test_ask_discussion import env as env  # noqa: F401
from tests.test_discord_inbound_router import GUILD, MESSAGE, message, router
from tests.test_escalation_intake import BOT_USER_ID, CHANNEL, HUMAN
from tests.test_review_commands import _scoped
from tests.test_review_commands import env as review_env  # noqa: F401


async def enable(handler, monkeypatch):
    handler.config.discord.guild_id = GUILD
    handler.config.discord.channel_id = CHANNEL
    handler.config.discord.project_id = "p"
    handler.config.discord.authorized_users = [HUMAN]
    handler.config.discord.conversation.enabled = True
    from src.asks.delivery import config_fingerprint, destination

    async with handler.db.immediate() as conn:
        await handler.db.advance_ask_mode(
            {
                "project_id": "p",
                "destination": destination(handler.config),
                "mode": "channel",
                "config_fingerprint": config_fingerprint(handler.config),
                "updated_at": 1,
            },
            expected_generation=0,
            conn=conn,
        )


def envelope(text, ident=MESSAGE):
    return {
        "transport": "discord",
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "external_message_id": ident,
        "external_root_message_id": ident,
        "author_id": HUMAN,
        "text": text,
        "received_at": 1000.0,
        "mentions_bot": False,
    }


async def admit(handler, text, ident=MESSAGE, source="gateway"):
    with principal_context(ExecutionPrincipal.service("discord-gateway")):
        return await handler.execute(
            "ask_intake", {"envelope": envelope(text, ident), "source": source}
        )


async def test_gateway_backfill_replay_and_conflict_have_one_owner(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    _, ask = await ask_for(handler)
    text = f"Explain {ask['id']}"
    one, two = await asyncio.gather(admit(handler, text), admit(handler, text, source="backfill"))
    assert one["success"] and two["success"], (one, two)
    assert one["input_id"] == two["input_id"]
    assert sum([one["created"], two["created"]]) == 1
    conflict = await admit(handler, "Different human words")
    assert conflict["error_code"] == "ask.input_conflict", conflict
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(conversation_inputs)) == 1
        assert await conn.scalar(select(func.count()).select_from(ask_reconciliation_cases)) == 1
        assert (await db.get_ask_input(one["input_id"], conn=conn))["text"] == text


async def test_failed_consumer_stays_queued_and_replay_does_not_dispatch(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    from src.asks import intake

    route = AsyncMock(side_effect=RuntimeError("consumer failed"))
    monkeypatch.setattr(intake, "route", route)
    first = await admit(handler, "Hello")
    replay = await admit(handler, "Hello", source="backfill")
    assert first["receipt"]["state"] == replay["receipt"]["state"] == "queued"
    assert route.await_count == 1


async def test_maintenance_retries_failed_input_without_starving_later_work(env, monkeypatch):
    from src.asks import intake

    handler, db = env
    await enable(handler, monkeypatch)
    real_route = intake.route
    monkeypatch.setattr(intake, "route", AsyncMock(side_effect=RuntimeError("consumer failed")))
    first = await admit(handler, "First question")
    second = await admit(handler, "Second question", ident=str(int(MESSAGE) + 1))

    async def retry(handler, item):
        if item["id"] == first["input_id"]:
            raise RuntimeError("Still unavailable")
        return await real_route(handler, item)

    monkeypatch.setattr(intake, "route", retry)
    failed = await handler.execute("ask_reconcile", {"apply": True, "limit": 1})
    assert failed["result"]["failed"] == [first["input_id"]]
    recovered = await handler.execute("ask_reconcile", {"apply": True, "limit": 1})
    assert recovered["result"]["routed"] == 1 and not recovered["result"]["failed"]
    async with db.immediate() as conn:
        item = await db.get_ask_input(second["input_id"], conn=conn)
        assert item["routing_state"] == "conversation"


async def test_two_pending_asks_and_yes_clarify_without_deciding(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    await ask_for(handler)
    await ask_for(handler)
    result = await admit(handler, "yes")
    assert result["receipt"]["state"] == "clarification", result
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(ask_decisions)) == 0
    assert (await admit(handler, "yes"))["receipt"] == result["receipt"]


async def test_clear_explicit_intent_applies_once_as_stored_human(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    rid, ask = await ask_for(handler)
    result = await admit(handler, f"yes {ask['id']}")
    assert result["receipt"]["state"] == "queued", result
    args = {
        "ask_id": ask["id"],
        "input_id": result["input_id"],
        "expected": ask["expected"],
        "option_key": "approve",
        "interpretation": "Human clearly approved this request",
        "idempotency_key": "interpret",
    }
    first = await _scoped(handler, "ask_apply_input", args, session_id="supervisor")
    assert first["success"], first
    second = await _scoped(handler, "ask_apply_input", args, session_id="supervisor")
    assert second["success"] and not second["created"], second
    assert (await db.get_review(rid))["state"] == "approved"
    async with db.immediate() as conn:
        decision = (await conn.execute(select(ask_decisions))).mappings().one()
        assert decision["actor_id"] == f"human:discord:{HUMAN}"
        assert decision["actor"]["provenance"]["interpreted_by"] == "supervisor"


async def test_router_admits_without_mention_before_legacy_consumer(env, monkeypatch):
    handler, _ = env
    await enable(handler, monkeypatch)
    intake = SimpleNamespace(handle=AsyncMock(), handle_detailed=AsyncMock())
    inbound, _, _ = router(handler, config=handler.config, intake=intake)
    assert await inbound.route(message(mention=False), bot_user_id=BOT_USER_ID) == "posted:created"
    intake.handle.assert_not_awaited()
    intake.handle_detailed.assert_not_awaited()


async def test_ordinary_shared_input_reuses_addressed_supervisor_reply(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    handler.orchestrator.conversation_outbox = SimpleNamespace(bound=True, enqueue=AsyncMock())
    admitted = await admit(handler, "Hello, can you explain the current work?")
    reply = await _scoped(
        handler,
        "supervisor_inbox_reply",
        {
            "conversation_id": admitted["receipt"]["conversation_id"],
            "input_id": admitted["input_id"],
            "text": "Here is the current work.",
            "idempotency_key": "ordinary-answer",
        },
        session_id="supervisor",
    )
    assert reply["success"], reply
    assert handler.orchestrator.conversation_outbox.enqueue.await_count == 1


@pytest.mark.parametrize(
    "text, expected",
    [
        ("yes", "approve"),
        ("no", "decline"),
        ("yes, if the document includes the failure case", "fold_in"),
        ("yes but approve the second too", None),
        ("yes and no", None),
    ],
)
def test_intent_requires_clear_words(text, expected):
    assert clear_intent(text)[0] == expected


async def test_conditional_approval_saves_formal_feedback_and_folds_in(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    rid, ask = await ask_for(handler)
    original = f"yes {ask['id']}, if the document includes a recovery procedure"
    result = await admit(handler, original)
    applied = await _scoped(
        handler,
        "ask_apply_input",
        {
            "ask_id": ask["id"],
            "input_id": result["input_id"],
            "expected": ask["expected"],
            "option_key": "fold_in",
            "interpretation": "Conditional approval requires revision",
            "idempotency_key": "conditional",
        },
        session_id="supervisor",
    )
    assert applied["success"], applied
    comments = await db.list_review_comments(rid)
    assert len(comments) == 1 and comments[0]["body"] == original
    assert comments[0]["source_discussion_id"]
    assert (await db.get_review(rid))["state"] == "changes_requested"


async def test_revoke_and_stale_source_refuse_interpreted_decision(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    _, ask = await ask_for(handler)
    result = await admit(handler, f"yes {ask['id']}")
    args = {
        "ask_id": ask["id"],
        "input_id": result["input_id"],
        "expected": ask["expected"],
        "option_key": "approve",
        "interpretation": "Yes",
        "idempotency_key": "interpret",
    }
    handler.config.discord.authorized_users = []
    refused = await _scoped(handler, "ask_apply_input", args, session_id="supervisor")
    assert refused["error_code"] == "ask.not_decider", refused
    handler.config.discord.authorized_users = [HUMAN]
    wrong = {**args, "expected": {**args["expected"], "decision_generation": 99}}
    stale = await _scoped(handler, "ask_apply_input", wrong, session_id="supervisor")
    assert stale["error_code"] == "ask.stale_source"
    async with db.immediate() as conn:
        assert await conn.scalar(select(func.count()).select_from(ask_decisions)) == 0


async def test_unique_clear_unbound_answer_routes_to_one_eligible_ask(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    _, ask = await ask_for(handler)
    result = await admit(handler, "yes")
    async with db.immediate() as conn:
        item = await db.get_ask_input(result["input_id"], conn=conn)
        assert item["ask_id"] == ask["id"] and item["routing_state"] == "interpretation"


async def test_live_length_limit_is_independent_of_historical_storage(env, monkeypatch):
    handler, _ = env
    await enable(handler, monkeypatch)
    refused = await admit(handler, "x" * 4001)
    assert refused["error_code"] == "ask.invalid_input"


@pytest.mark.parametrize("change", ["private", "revoked", "quarantined"])
async def test_queued_agent_answer_rechecks_public_audience(env, monkeypatch, change):
    from sqlalchemy import update
    from src.conversations.delivery import ConversationDeliveryAdapter
    from src.database.tables import human_asks, outbound_deliveries

    handler, db = env
    await enable(handler, monkeypatch)
    _, ask = await ask_for(handler)
    result = await admit(handler, f"Explain {ask['id']}")
    answered = await _scoped(
        handler,
        "ask_discussion_reply",
        {
            "ask_id": ask["id"],
            "input_id": result["input_id"],
            "text": "The private answer",
            "idempotency_key": "answer",
        },
        session_id="supervisor",
    )
    assert answered["success"], answered
    async with db.immediate() as conn:
        row = dict(
            (
                await conn.execute(
                    select(outbound_deliveries).where(
                        outbound_deliveries.c.id == answered["delivery_id"],
                    )
                )
            )
            .mappings()
            .one()
        )
        if change == "private":
            await conn.execute(
                update(human_asks)
                .where(human_asks.c.id == ask["id"])
                .values(audience={"private": True})
            )
        elif change == "revoked":
            handler.config.discord.authorized_users = []
        else:
            await db.update_ask_input(
                result["input_id"], {"routing_state": "quarantined"}, conn=conn
            )
    transport = SimpleNamespace(send=AsyncMock())
    adapter = ConversationDeliveryAdapter(db, transport, config=handler.config, lease_owner="test")
    adapter._finish = AsyncMock()
    await adapter.deliver(row)
    assert adapter._finish.await_args.kwargs["status"] == "cancelled"
    transport.send.assert_not_awaited()


async def test_legacy_import_keeps_16000_characters_and_original_identity(env):
    from sqlalchemy import insert
    from src.database.tables import escalation_messages, escalations

    handler, db = env
    async with db.immediate() as conn:
        await conn.execute(
            insert(escalations).values(
                id="legacy",
                project_id="p",
                source_kind="core",
                source_identity="source",
                incident_key="source",
                supervisor_owner="supervisor:p",
                summary="Question",
                investigation="Checked",
                decision_requested="Proceed?",
                severity="high",
                created_at=1,
                updated_at=1,
            )
        )
        await conn.execute(
            insert(escalation_messages).values(
                id="legacy-message",
                escalation_id="legacy",
                direction="inbound",
                transport="discord",
                verified_actor=f"discord:{HUMAN}",
                text="x" * 16000,
                external_message_id=MESSAGE,
                received_at=1,
                created_at=1,
            )
        )
    result = await handler.execute("ask_reconcile", {"apply": True})
    assert result["success"] and result["result"]["imported"] == 1, result
    async with db.immediate() as conn:
        item = (await conn.execute(select(conversation_inputs))).mappings().one()
        assert item["text"] == "x" * 16000 and item["external_message_id"] == MESSAGE
        assert item["verified_actor"] == f"human:discord:{HUMAN}"
        assert item["retention_entry_id"] == "legacy-message" and item["guild_id"] is None
    replay = await handler.execute("ask_reconcile", {"apply": True})
    assert replay["result"]["imported"] == 0


async def test_legacy_thread_window_one_move_notice_and_rollback(env, monkeypatch):
    import time
    from sqlalchemy import update
    from src.asks.delivery import AskDeliveryAdapter, destination
    from src.database.tables import human_communication_modes, outbound_deliveries
    from src.escalations.transport import SinkTransport

    handler, db = env
    await enable(handler, monkeypatch)
    _, ask = await ask_for(handler)
    handler.ask_delivery = AskDeliveryAdapter(handler, SinkTransport(), lease_owner="test")
    async with db.immediate() as conn:
        await conn.execute(
            update(human_communication_modes).values(compatibility_deadline=time.time() + 100)
        )
        await db.insert_ask_binding(
            {
                "id": "old-thread-binding",
                "ask_id": ask["id"],
                "project_id": "p",
                "destination": destination(handler.config),
                "transport": "discord",
                "guild_id": GUILD,
                "channel_id": CHANNEL,
                "external_root_id": "232323232323232323",
                "legacy_thread_id": "454545454545454545",
                "mode_generation": 1,
                "root_generation": 1,
                "root_state": "confirmed",
                "receipt": {"legacy_delivery_id": "old"},
                "created_at": 1,
                "updated_at": 1,
            },
            conn=conn,
        )

    async def reply(n):
        payload = {
            **envelope("Explain this request", ident=str(int(MESSAGE) + n)),
            "external_thread_id": "454545454545454545",
        }
        with principal_context(ExecutionPrincipal.service("discord-gateway")):
            return await handler.execute("ask_intake", {"envelope": payload})

    first, second = await reply(0), await reply(1)
    assert first["success"] and second["success"], (first, second)
    async with db.immediate() as conn:
        item = await db.get_ask_input(first["input_id"], conn=conn)
        assert item["ask_id"] == ask["id"] and item["routing_state"] == "discussion"
        notices = (
            (
                await conn.execute(
                    select(outbound_deliveries).where(
                        outbound_deliveries.c.send_kind == "move_notice"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(notices) == 1
        assert "reference_message_id" not in notices[0]["payload"]
        await conn.execute(
            update(human_communication_modes).values(compatibility_deadline=time.time() - 1)
        )
    expired = await reply(2)
    async with db.immediate() as conn:
        item = await db.get_ask_input(expired["input_id"], conn=conn)
        assert item["routing_state"] == "clarification"
        assert not await conn.scalar(select(func.count()).select_from(ask_decisions))
    disabled = await handler.execute(
        "ask_cutover",
        {
            "project_id": "p",
            "mode": "delivery_disabled",
            "expected_generation": 1,
        },
    )
    assert disabled["success"], disabled
    assert (await reply(3))["error_code"] == "ask.channel_disabled"


async def test_rollback_rejects_already_queued_external_intent(env, monkeypatch):
    handler, db = env
    await enable(handler, monkeypatch)
    _, ask = await ask_for(handler)
    admitted = await admit(handler, f"yes {ask['id']}")
    disabled = await handler.execute(
        "ask_cutover",
        {
            "project_id": "p",
            "mode": "delivery_disabled",
            "expected_generation": 1,
        },
    )
    assert disabled["success"], disabled
    result = await _scoped(
        handler,
        "ask_apply_input",
        {
            "ask_id": ask["id"],
            "input_id": admitted["input_id"],
            "expected": ask["expected"],
            "option_key": "approve",
            "interpretation": "Explicit approval",
            "idempotency_key": "paused-input",
        },
        session_id="supervisor",
    )
    assert not result["success"] and result["error_code"] == "ask.channel_disabled", result
    async with db.immediate() as conn:
        assert not await conn.scalar(select(func.count()).select_from(ask_decisions))
