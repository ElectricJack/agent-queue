"""Collaboration sends and replies, safe delivery, and rollover prime context."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy import update

from src.api.messages import MessageSendRequest
from src.database.tables import sessions, tasks
from src.event_bus import EventBus
from src.messages.delivery import MessageDeliveryEngine, _render_nudge
from src.models import TaskStatus
from src.prime.sections import build_messages_section
from tests.test_collaboration_commands import (
    SUPERVISOR,
    accept,
    create,
    env as command_env,  # noqa: F401 - shared pytest fixture
    run,
    worker,
)
from tests.test_message_delivery import FakeSessionManager


@pytest.fixture
async def env(command_env, monkeypatch):  # noqa: F811 - pytest injects the shared fixture
    from src.sessions.provider import SessionProvider

    pane_input = AsyncMock()
    monkeypatch.setattr(SessionProvider, "send_input", pane_input)
    for profile_id in ("worker-codex", "supervisor"):
        profile = await command_env.db.get_profile(profile_id)
        await command_env.db.update_profile(
            profile_id,
            aq_commands=[*profile.aq_commands, "message_send", "message_reply", "message_wait"],
        )
    command_env.bus = EventBus(env="dev")
    command_env.commands.orchestrator.bus = command_env.bus
    command_env.db.touch_session_activity = AsyncMock()
    command_env.commands._cmd_session_input = AsyncMock()
    yield command_env
    command_env.db.touch_session_activity.assert_not_called()
    command_env.commands._cmd_session_input.assert_not_called()
    pane_input.assert_not_called()


async def joined(env, members=("one", "two")):
    thread = await create(env, members=members)
    for task_id in members:
        await accept(env, thread, task_id)
    return thread


async def send(env, thread, sender="one", **extra):
    return await run(
        env,
        "message_send",
        {"thread_id": thread["id"], "body": "Review commit abc123", "claim_epoch": 1, **extra},
        worker(sender),
    )


async def test_fanout_uses_one_seq_and_server_sender_ignoring_from_args(env):
    thread = await joined(env, ("one", "two", "four"))
    events = []
    env.bus.subscribe("message.sent", lambda data: events.append(dict(data)))
    result = await send(env, thread, from_kind="invented", from_id="forged", client_key="k")
    assert result["state"] == "queued" and result["seq"] == 1, result
    assert len(result["message_ids"]) == 2
    copies = [await env.db.get_message(mid) for mid in result["message_ids"]]
    assert {m.to_id for m in copies} == {"two", "four"}
    assert all(m.to_kind == "task" and m.from_kind == "session" for m in copies)
    assert all(m.from_id == "s-one" and m.thread_id == thread["id"] for m in copies)
    assert all(m.body_kind == "collaboration" for m in copies)
    assert {e["message_id"] for e in events} == set(result["message_ids"])
    assert all(e["thread_id"] == thread["id"] for e in events)
    replay = await send(env, thread, client_key="k")
    assert replay["replayed"] and replay["seq"] == 1
    assert replay["message_ids"] == result["message_ids"]
    assert len(events) == 2  # a replay adds no delivery intent


async def test_explicit_recipient_must_be_another_member_task(env):
    thread = await joined(env, ("one", "two", "four"))
    sent = await send(env, thread, to_kind="task", to_id="four")
    assert len(sent["message_ids"]) == 1, sent
    assert (await env.db.get_message(sent["message_id"])).to_id == "four"
    for args in (
        {"to_kind": "session", "to_id": "s-two"},
        {"to_kind": "task", "to_id": "foreign"},
        {"to_kind": "task", "to_id": "one"},
        {"to_kind": "task"},
    ):
        refused = await send(env, thread, **args)
        assert "error" in refused and refused["error_code"].startswith("collaboration.")


async def test_nonmembers_unknown_threads_and_elevated_senders_are_refused(env):
    thread = await joined(env)
    assert "error" in await send(env, thread, sender="four")
    assert "error" in await send(env, {"id": "collab-unknown"})
    for scope in (None, SUPERVISOR):
        result = await run(env, "message_send", {"thread_id": thread["id"], "body": "x"}, scope)
        assert result["error_code"] == "collaboration.out_of_scope"


async def test_rollover_requires_reaccept_and_rejects_old_claim(env):
    thread = await joined(env)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "one").values(claim_epoch=2))
        await conn.execute(
            update(sessions).where(sessions.c.id == "s-one").values(last_claim_epoch=2)
        )
    stale = await send(env, thread)
    assert stale["error_code"] == "collaboration.stale_claim"
    unaccepted = await send(env, thread, claim_epoch=2)
    assert unaccepted["error_code"] == "collaboration.not_accepted"
    await accept(env, thread, epoch=2)
    assert (await send(env, thread, claim_epoch=2))["seq"] == 1


async def test_reply_uses_the_thread_and_marks_original_read_only_on_success(env):
    thread = await joined(env)
    sent = await send(env, thread)
    refused = await run(
        env,
        "message_reply",
        {"message_id": sent["message_id"], "body": "x", "claim_epoch": 1},
        worker("four"),
    )
    assert "error" in refused
    assert (await env.db.get_message(sent["message_id"])).read_at is None
    result = await run(
        env,
        "message_reply",
        {
            "message_id": sent["message_id"],
            "body": "Looks good",
            "claim_epoch": 1,
            "from_kind": "system",
            "from_id": "forged",
            "client_key": "reply",
        },
        worker("two"),
    )
    assert result["seq"] == 2, result
    reply = await env.db.get_message(result["reply_id"])
    assert reply.thread_id == thread["id"] and reply.reply_to_id == sent["message_id"]
    assert reply.from_id == "s-two" and reply.to_id == "one"
    assert (await env.db.get_message(sent["message_id"])).read_at is not None


async def test_ordinary_send_cannot_link_a_collaboration_reply(env):
    thread = await joined(env)
    sent = await send(env, thread)
    result = await run(
        env,
        "message_send",
        {
            "project_id": "p",
            "from_kind": "session",
            "from_id": "s-two",
            "to_kind": "task",
            "to_id": "one",
            "body": "bypass",
            "thread_id": "ordinary",
            "reply_to_id": sent["message_id"],
        },
        worker("two"),
    )
    assert result["error_code"] == "collaboration.invalid"


async def test_http_send_fanout_and_typed_reply_preserve_collaboration_fields(env):
    import httpx
    from fastapi import FastAPI

    from src.api.auth import RequestScope
    from src.api.codegen import build_category_routers
    from src.api.dependencies import get_command_handler
    from src.api.messages import router

    thread = await joined(env)
    app = FastAPI()
    app.include_router(router)
    for category in build_category_routers():
        if category.prefix == "/api/message":
            app.include_router(category)
    app.dependency_overrides[get_command_handler] = lambda: env.commands
    owner = "one"

    @app.middleware("http")
    async def scoped(request, call_next):
        request.state.scope = RequestScope(**worker(owner))
        return await call_next(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/messages/send",
            json={
                "thread_id": thread["id"],
                "body": "finding",
                "client_key": "http",
                "claim_epoch": 1,
            },
        )
        assert response.status_code == 200, response.text
        sent = response.json()
        assert sent["seq"] == 1 and len(sent["message_ids"]) == 1
        owner = "two"
        response = await client.post(
            "/api/message/reply",
            json={
                "message_id": sent["message_id"],
                "body": "thanks",
                "claim_epoch": 1,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["seq"] == 2
        assert response.json()["reply"]["thread_id"] == thread["id"]


async def test_client_key_and_body_limits_and_rate_delay(env):
    thread = await joined(env)
    for args, code in (
        ({"client_key": "x" * 129}, "collaboration.invalid"),
        ({"body": "💡" * 1025}, "collaboration.message_too_large"),
        ({"body": "   "}, "collaboration.invalid"),
    ):
        assert (await send(env, thread, **args))["error_code"] == code
    for index in range(5):
        assert (await send(env, thread, client_key=str(index)))["seq"] == index + 1
    limited = await send(env, thread)
    assert limited["error_code"] == "collaboration.rate_limited"
    assert 0 < limited["retry_after"] <= 60


@pytest.mark.parametrize(
    "body_kind", ["collaboration", "collaboration_invite", "collaboration_closed"]
)
async def test_delivery_busy_then_idle_paused_sleeping_and_no_tail_reply(env, body_kind):
    # Exercise each policy on the actual messages substrate.
    msg = await env.db.create_message(
        project_id="p",
        from_kind="session",
        from_id="s-one",
        to_kind="task",
        to_id="two",
        body="finding",
        thread_id="collab-delivery",
        body_kind=body_kind,
    )
    lens = FakeSessionManager(activity_map={("task", "two", "p"): "busy"})
    engine = MessageDeliveryEngine(env.db, lens, env.commands.config, env.bus)
    await engine.run_delivery_pass()
    assert (await env.db.get_message(msg.id)).delivered_at is None
    assert lens.nudges == []
    lens.activity_map[("task", "two", "p")] = "sleeping"
    await engine.run_delivery_pass()
    assert lens.ensure_started_calls == []
    lens.activity_map[("task", "two", "p")] = "idle"
    await env.db.update_task("two", status=TaskStatus.PAUSED)
    await engine.run_delivery_pass()
    assert lens.nudges == []
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "two").values(status="IN_PROGRESS"))
    await engine.run_delivery_pass()
    assert lens.nudges[-1][-1] == "Handle `aq collaboration show collab-delivery --json`."
    assert (await env.db.get_message(msg.id)).delivered_at is not None
    async with env.db._engine.begin() as conn:
        from src.database.tables import messages

        await conn.execute(
            update(messages).where(messages.c.id == msg.id).values(delivered_at=time.time() - 300)
        )
    lens.tail_map[("task", "two", "p")] = "Unrelated assistant output"
    lens.tail_assistant_turn = AsyncMock(return_value="Unrelated assistant output")
    assert await engine.check_reply_timeouts() == 0
    lens.tail_assistant_turn.assert_not_called()
    assert _render_nudge([msg]) == lens.nudges[-1][-1]


async def test_prime_lists_active_threads_and_rollover_accept_hint(env):
    thread = await joined(env)
    await env.db.update_task("two", status=TaskStatus.READY)
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "one").values(claim_epoch=2))
    section = await build_messages_section(
        env.db, "one", config=env.commands.config, task=await env.db.get_task("one")
    )
    assert thread["id"] in section.body and "Review the diff" in section.body
    assert "last_seq: 0" in section.body and "deadline" in section.body.lower()
    assert "not running" in section.body
    assert f"aq collaboration accept {thread['id']}" in section.body
    assert "before sending or waiting" in section.body


def test_http_request_accepts_collaboration_without_recipient():
    request = MessageSendRequest(
        thread_id="collab-http", body="finding", client_key="k", claim_epoch=1
    )
    assert request.to_kind is None and request.to_id is None
    assert request.client_key == "k" and request.claim_epoch == 1


def test_cli_collaboration_send_and_reply_resolve_claim_epoch():
    from src.cli.app import cli

    client = AsyncMock()
    client.execute.return_value = {"message_id": "m", "reply_id": "r", "state": "queued"}

    @asynccontextmanager
    async def get_client(*args):
        yield client

    with patch("src.cli.messages._get_client", get_client):
        result = CliRunner().invoke(
            cli,
            [
                "message",
                "send",
                "--thread-id",
                "collab-cli",
                "--body",
                "finding",
                "--client-key",
                "send-k",
                "--claim-epoch",
                "7",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["data"]["message_id"] == "m"
        params = client.execute.await_args.args[1]
        assert "to_kind" not in params and "to_id" not in params
        assert params["client_key"] == "send-k" and params["claim_epoch"] == 7
        result = CliRunner().invoke(
            cli,
            [
                "message",
                "reply",
                "m",
                "finding",
                "--claim-epoch",
                "7",
                "--client-key",
                "reply-k",
            ],
        )
        assert result.exit_code == 0, result.output
        assert client.execute.await_args.args[1]["claim_epoch"] == 7
        assert client.execute.await_args.args[1]["client_key"] == "reply-k"
