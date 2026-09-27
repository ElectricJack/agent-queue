"""Bounded message long-poll on the same durable wait, with lossless cursors."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy import update

from src.commands.contracts import CONTRACTS
from src.database.tables import agent_waits, sessions, tasks
from tests.test_collaboration_commands import accept, run, worker
from tests.test_collaboration_messages import command_env as command_env, env as env, joined, send


async def wait(env, thread, after=0, **extra):
    return await run(
        env,
        "message_wait",
        {"thread_id": thread["id"], "after_seq": after, "timeout": 1, "claim_epoch": 1, **extra},
        worker(),
    )


async def test_unseen_messages_return_immediately_with_thread_cursor(env):
    thread = await joined(env)
    await send(env, thread, "two")
    started = time.monotonic()
    result = await wait(env, thread)
    assert result.get("state") == "satisfied", result
    assert time.monotonic() - started < 1
    assert [m["seq"] for m in result["messages"]] == [1]
    assert result["next_cursor"] == 1
    assert result["wait"]["kind"] == "message"
    assert result["wait"]["match"] == {"thread_id": thread["id"], "after_seq": 0}
    assert result["wait"]["deadline_at"] == thread["deadline_at"]


async def test_concurrent_send_wakes_under_five_seconds(env):
    thread = await joined(env)
    task = asyncio.create_task(wait(env, thread, timeout=5))
    try:
        for _ in range(100):
            if env.bus.subscriber_count("message.sent"):
                break
            await asyncio.sleep(0.01)
        started = time.monotonic()
        await send(env, thread, "two")
        result = await asyncio.wait_for(task, timeout=4.5)
        assert result["state"] == "satisfied", result
        assert time.monotonic() - started < 5
        assert result["next_cursor"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert env.bus.subscriber_count("message.sent") == 0


async def test_timeout_keeps_wait_active_and_cursor_then_reconnect_reuses_it(env):
    thread = await joined(env)
    first = await wait(env, thread)
    assert first.get("state") == "waiting", first
    assert first["cursor"] == 0 and "End this turn" in first["next_step"]
    row = await env.db.get_agent_wait(first["wait"]["id"])
    assert row["state"] == "active"
    assert first["wait"]["id"] in first["next_step"]
    await send(env, thread, "two", client_key="between")
    replay = await wait(env, thread)
    assert replay["state"] == "satisfied", replay
    assert replay["wait"]["id"] == row["id"]
    assert replay["wait"]["deadline_at"] == row["deadline_at"]
    assert [m["seq"] for m in replay["messages"]] == [1]
    next_page = await wait(env, thread, after=replay["next_cursor"])
    assert next_page["state"] == "waiting" and next_page["cursor"] == 1


async def test_disconnect_keeps_wait_and_cleans_subscription(env):
    thread = await joined(env)
    task = asyncio.create_task(wait(env, thread, timeout=60))
    for _ in range(100):
        rows = await env.db.list_agent_waits(project_id="p", owner_id="one")
        if rows:
            break
        await asyncio.sleep(0.01)
    assert rows
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert env.bus.subscriber_count("message.sent") == 0
    assert (await env.db.get_agent_wait(rows[0]["id"]))["state"] == "active"
    await send(env, thread, "two")
    assert (await wait(env, thread))["next_cursor"] == 1


async def test_reassigned_pool_task_cannot_consume_the_old_wait_at_same_epoch(env):
    thread = await joined(env, ("one", "two", "four"))
    task = asyncio.create_task(wait(env, thread, timeout=5))
    try:
        for _ in range(100):
            rows = await env.db.list_agent_waits(project_id="p", owner_id="one")
            if rows:
                break
            await asyncio.sleep(0.01)
        assert rows
        async with env.db._engine.begin() as conn:
            await conn.execute(
                update(tasks).where(tasks.c.id == "four").values(assigned_agent_id="agent-one")
            )
            await conn.execute(
                update(sessions).where(sessions.c.id == "s-one").values(task_id="four")
            )
        await send(env, thread, "two")
        result = await asyncio.wait_for(task, timeout=4.5)
        assert result["error_code"] == "collaboration.stale_claim", result
        assert "messages" not in result
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "count,body,expected",
    [
        pytest.param(24, "small", 20, id="message-bound"),
        pytest.param(9, "x" * 4096, 8, id="byte-bound"),
    ],
)
async def test_read_bounds_never_advance_past_unseen_messages(env, count, body, expected):
    base = time.time() - count * 61
    thread = await env.db.create_collaboration_thread(
        project_id="p",
        created_by_kind="operator",
        created_by_id="operator",
        idempotency_key="bounded",
        task_ids=["one", "two"],
        goal="Bounded reads",
        deadline_seconds=7200,
        message_budget=40,
        now=base - 1,
    )
    for task_id in ("one", "two"):
        await env.db.accept_collaboration(
            thread_id=thread["id"],
            project_id="p",
            task_id=task_id,
            claim_epoch=1,
            now=base,
        )
    for index in range(count):
        await env.db.append_collaboration_message(
            thread_id=thread["id"],
            project_id="p",
            sender_task_id="two",
            sender_claim_epoch=1,
            sender_session_id="s-two",
            client_key=str(index),
            body=body,
            now=base + index * 61,
        )
    result = await wait(env, thread)
    assert result["state"] == "satisfied", result
    assert [m["seq"] for m in result["messages"]] == list(range(1, expected + 1))
    assert result["next_cursor"] == expected and result["has_more"]
    page = await wait(env, thread, after=result["next_cursor"])
    assert page["state"] == "satisfied", page
    assert page["messages"][0]["seq"] == expected + 1
    assert page["next_cursor"] == count


@pytest.mark.parametrize(
    "extra", [{"timeout": 61}, {"timeout": 0}, {"timeout": True}, {"after_seq": -1}]
)
async def test_invalid_bounds_refused_without_registering(env, extra):
    thread = await joined(env)
    result = await wait(env, thread, **extra)
    assert "error" in result and result["error_code"] == "collaboration.invalid", result
    assert await env.db.list_agent_waits(project_id="p", owner_id="one") == []


async def test_other_threads_name_the_detached_message_wait(env):
    result = await wait(env, {"id": "ordinary-chat"})
    assert "error" in result
    assert "aq wait register --kind message" in result["error"]


async def test_http_long_poll_has_typed_messages_and_refusal(env):
    import httpx
    from fastapi import FastAPI

    from src.api.auth import RequestScope
    from src.api.codegen import build_category_routers
    from src.api.dependencies import get_command_handler

    thread = await joined(env)
    await send(env, thread, "two")
    app = FastAPI()
    for router in build_category_routers():
        if router.prefix == "/api/message":
            app.include_router(router)
    app.dependency_overrides[get_command_handler] = lambda: env.commands

    @app.middleware("http")
    async def scoped(request, call_next):
        request.state.scope = RequestScope(**worker())
        return await call_next(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/message/wait",
            json={
                "thread_id": thread["id"],
                "after_seq": 0,
                "claim_epoch": 1,
                "timeout": 1,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["messages"][0]["seq"] == 1
        assert response.json()["next_cursor"] == 1
        response = await client.post(
            "/api/message/wait",
            json={
                "thread_id": "ordinary",
                "after_seq": 0,
                "claim_epoch": 1,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["error_code"] == "collaboration.invalid"


@pytest.mark.parametrize(
    "reason", ["thread_closed", "peer_failed", "peer_gone", "partner_not_running"]
)
async def test_typed_resolution_states(env, reason):
    thread = await joined(env)
    if reason == "thread_closed":
        await env.db.close_collaboration_thread(
            thread_id=thread["id"], project_id="p", reason="closed", now=time.time()
        )
    elif reason == "peer_failed":
        await env.db.update_task("two", status="FAILED")
    elif reason == "peer_gone":
        await env.db.update_task("two", status="COMPLETED")
    else:
        first = await wait(env, thread)
        async with env.db._engine.begin() as conn:
            await conn.execute(update(tasks).where(tasks.c.id == "two").values(status="READY"))
            await conn.execute(
                update(agent_waits)
                .where(agent_waits.c.id == first["wait"]["id"])
                .values(created_at=time.time() - 121)
            )
    result = await wait(env, thread)
    assert result["state"] == reason, result
    assert result["next_cursor"] == 0


async def test_stale_unaccepted_and_nonmember_waits_refused(env):
    thread = await joined(env)
    assert "error" in await run(
        env,
        "message_wait",
        {"thread_id": thread["id"], "after_seq": 0, "claim_epoch": 1},
        worker("four"),
    )
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "one").values(claim_epoch=2))
        await conn.execute(
            update(sessions).where(sessions.c.id == "s-one").values(last_claim_epoch=2)
        )
    assert (await wait(env, thread))["error_code"] == "collaboration.stale_claim"
    assert "error" in await wait(env, thread, claim_epoch=2)
    await accept(env, thread, epoch=2)
    assert (await wait(env, thread, claim_epoch=2))["state"] == "waiting"


def test_contract_scope_tool_grants_and_cli_timeout():
    from pathlib import Path

    from src.api.scope import AGENT_COMMAND_SET
    from src.cli.client import _COMMAND_TIMEOUTS
    from src.profiles.parser import parse_profile
    from src.tools.registry import ToolRegistry

    assert CONTRACTS.require("message_wait").contract.execution.args_model.model_fields["after_seq"]
    assert "message_wait" in AGENT_COMMAND_SET
    assert _COMMAND_TIMEOUTS["message_wait"] == 90
    assert any(t["name"] == "message_wait" for t in ToolRegistry().get_category_tools("message"))
    for name in ("worker-codex", "worker-claude", "supervisor"):
        path = Path("src/profiles/defaults") / name / "profile.md"
        profile = parse_profile(path.read_text())
        assert "message_wait" in profile.capabilities["aq_commands"]


def test_cli_wait_has_json_envelope_and_claim_epoch():
    from src.cli.app import cli

    client = AsyncMock()
    client.execute.return_value = {"state": "waiting", "wait": {"id": "w"}, "cursor": 4}

    @asynccontextmanager
    async def get_client(*args):
        yield client

    with patch("src.cli.message_wait._get_client", get_client):
        result = CliRunner().invoke(
            cli,
            [
                "message",
                "wait",
                "--thread",
                "collab-cli",
                "--after",
                "4",
                "--timeout",
                "60",
                "--claim-epoch",
                "7",
                "--json",
            ],
        )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["data"]["cursor"] == 4
    name, params = client.execute.await_args.args
    assert name == "message_wait" and params["after_seq"] == 4 and params["claim_epoch"] == 7
