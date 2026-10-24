"""Initiated supervisor posts use the conversation outbox without consuming input."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from src.api.auth import RequestScope
from src.api.scope import check_request_scope
from src.commands.principal import principal_context
from src.conversations.intake import DM_GUILD, dm_thread_id
from src.models import Project
from src.profiles.capabilities import CapabilityPolicy
from src.escalations.transport import SinkTransport
from tests.test_conversation_wiring import DM_CHANNEL, bind, supervisor
from tests.test_supervisor_inbox_commands import AUTHOR, NOW, args, post, reply, reply_args
from tests.test_supervisor_inbox_commands import env as env  # noqa: F401


async def say(handler, values, principal):
    with principal_context(principal):
        return await handler.execute("supervisor_inbox_say", values)


async def ready(handler, db, project_id=None, *, kind="channel"):
    if project_id:
        await db.create_project(Project(id=project_id, name=project_id))
        handler.config.discord.project_id = project_id
    _, principal = await supervisor(db, project_id)
    principal = replace(principal, policy=CapabilityPolicy.from_namespaces(
        aq_commands=["supervisor_inbox_say", "supervisor_inbox_reply"],
    ))
    handler.config.discord.conversation.require_mention = kind == "thread"
    handler.config.discord.conversation.allow_dm = kind == "dm"
    transport = SinkTransport()
    service = bind(handler, db, transport)
    incoming = args()
    if kind == "dm":
        incoming["envelope"].update(
            guild_id=DM_GUILD, channel_id=DM_CHANNEL,
            external_thread_id=dm_thread_id(DM_CHANNEL), mentions_bot=False,
        )
    first = await post(handler, incoming)
    assert first["success"], first
    await service.pump()
    return first, principal, service, transport


def values(first, key="question-two", text="What should happen next?"):
    return {"conversation_id": first["conversation_id"], "text": text, "idempotency_key": key}


@pytest.mark.parametrize("project_id", [None, "p"])
@pytest.mark.parametrize("kind", ["channel", "thread", "dm"])
async def test_initiated_post_after_answer_is_durable_concurrent_and_delivered_once(
    env, project_id, kind,
):
    handler, db = env
    first, principal, service, transport = await ready(handler, db, project_id, kind=kind)
    answered = await reply(handler, reply_args(first), principal)
    assert answered["success"], answered
    await service.pump()
    before = await db.get_conversation_input(first["input_id"])
    request = values(first)
    if project_id:
        scope = RequestScope(kind="session", session_id=principal.session_id,
                             project_id=project_id, elevated=True)
        assert await check_request_scope("supervisor_inbox_say", dict(request), scope, db=db) is None
    one, two = await asyncio.gather(say(handler, request, principal), say(handler, request, principal))
    assert one["success"] and two["success"], (one, two)
    assert one["created"] != two["created"]
    assert one["delivery_id"] == two["delivery_id"]
    replay = await say(handler, {**request, "text": "changed retry body"}, principal)
    assert replay["success"] and not replay["created"], replay
    rows = [r for r in await db.list_outbound_deliveries(
        owner_kind="conversation", owner_id=first["conversation_id"],
    ) if r["dedup_key"] == one["delivery_dedup_key"]]
    assert len(rows) == 1 and rows[0]["id"] == one["delivery_id"]
    assert rows[0]["payload"]["action"] == "reply"
    assert rows[0]["payload"]["supervisor_initiated"] is True
    assert "input_id" not in rows[0]["payload"]
    assert await db.get_conversation_input(first["input_id"]) == before
    assert (await db.get_message(one["reply_message_id"])).body == request["text"]
    await service.pump()
    await service.pump()
    posts = [m for m in transport.messages.values() if request["text"] in m.content]
    assert len(posts) == 1
    conversation = await db.get_conversation(first["conversation_id"])
    assert posts[0].where == (
        conversation["external_thread_id"] if kind == "thread" else conversation["channel_id"]
    )
    assert posts[0].thread_id == (conversation["external_thread_id"] if kind == "thread" else None)
    assert (await db.get_outbound_delivery(one["delivery_id"]))["external_receipt_id"] == posts[0].id


async def test_post_recovers_crash_before_enqueue_with_first_body(env, monkeypatch):
    handler, db = env
    first, principal, _, _ = await ready(handler, db)
    outbox = handler.orchestrator.conversation_outbox
    enqueue = outbox.enqueue
    monkeypatch.setattr(outbox, "enqueue", AsyncMock(side_effect=RuntimeError("crash")))
    failed = await say(handler, values(first, text="Original full text"), principal)
    assert not failed["success"]
    monkeypatch.setattr(outbox, "enqueue", enqueue)
    recovered = await say(handler, values(first, text="Changed text"), principal)
    assert recovered["success"] and not recovered["created"], recovered
    row = await db.get_outbound_delivery(recovered["delivery_id"])
    assert "Original full text" in row["payload"]["text"]
    assert "Changed text" not in row["payload"]["text"]


async def test_post_quotas_serialize_and_retry_does_not_spend_quota(env):
    handler, db = env
    first, principal, _, _ = await ready(handler, db)
    results = await asyncio.gather(*(
        say(handler, values(first, key=f"question-{i}"), principal) for i in range(11)
    ))
    assert sum(r["success"] for r in results) == 10, results
    denied = next(r for r in results if not r["success"])
    assert denied["error_code"] == "rate_limited" and denied["scope"] == "supervisor"
    first_key = next(i for i, r in enumerate(results) if r["success"])
    replay = await say(handler, values(first, key=f"question-{first_key}"), principal)
    assert replay["success"] and not replay["created"], replay
    handler._clock = lambda: NOW + 601
    assert (await say(handler, values(first, key="after-window"), principal))["success"]


async def test_channel_quota_is_enforced(env, monkeypatch):
    import src.database.queries.conversation_queries as queries

    handler, db = env
    first, principal, _, _ = await ready(handler, db)
    monkeypatch.setattr(queries, "SUPERVISOR_CHANNEL_WINDOW_LIMIT", 2)
    for key in ("one", "two"):
        assert (await say(handler, values(first, key=key), principal))["success"]
    denied = await say(handler, values(first, key="three"), principal)
    assert denied["error_code"] == "rate_limited" and denied["scope"] == "channel"


@pytest.mark.parametrize("failure", ["audience", "channel", "disabled", "closed", "blocked", "stale"])
async def test_post_refuses_changed_authority_and_visibility(env, failure):
    handler, db = env
    first, principal, _, _ = await ready(handler, db)
    if failure == "audience":
        handler.config.discord.authorized_users = [str(int(AUTHOR) + 1)]
    elif failure == "channel":
        handler.config.discord.channel_id = "999999999999999999"
    elif failure == "disabled":
        handler.config.discord.conversation.enabled = False
    elif failure in {"closed", "blocked"}:
        await db.set_conversation_state(first["conversation_id"],
                                        state="closed" if failure == "closed" else "delivery_blocked",
                                        now=NOW)
    else:
        principal = replace(principal, session_instance_token="old-launch")
    result = await say(handler, values(first), principal)
    assert not result["success"], result
    assert not [r for r in await db.list_outbound_deliveries(
        owner_kind="conversation", owner_id=first["conversation_id"],
    ) if r["dedup_key"].startswith("conv-say:")]


async def test_foreign_project_and_worker_cannot_initiate(env):
    handler, db = env
    first, _, _, _ = await ready(handler, db, "p")
    await db.create_project(Project(id="other", name="Other"))
    _, foreign = await supervisor(db, "other")
    foreign = replace(foreign, policy=CapabilityPolicy.from_namespaces(
        aq_commands=["supervisor_inbox_say"],
    ))
    scope = RequestScope(kind="session", session_id=foreign.session_id,
                         project_id="other", elevated=True)
    assert await check_request_scope("supervisor_inbox_say", values(first), scope, db=db)
    denied = await say(handler, values(first), foreign)
    assert denied["error_code"] == "out_of_scope", denied
    with principal_context(replace(foreign, elevated=False)):
        denied = await handler._cmd_supervisor_inbox_say(values(first))
    assert denied["error_code"] == "out_of_scope", denied
