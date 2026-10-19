"""CLI reports, authenticated return identities and durable replies on PostgreSQL."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from sqlalchemy import update

from src.api import dependencies
from src.api.auth import SessionTokenStore
from src.api.execute import router as execute_router
from src.api.messages import MessageSendRequest, SessionMessageRequest, router as message_router
from src.api.middleware import TokenAuthMiddleware
from src.api.scope import check_request_scope
from src.cli.app import cli
from src.cli.client import CLIClient
from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database.tables import agents, sessions, tasks
from src.models import AgentProfile, SessionRecord
from tests.test_agent_wait_queries import env as _wait_env

env = _wait_env


@pytest.fixture
async def api(env, monkeypatch):
    await env.db.update_project("p", supervisor_enabled=True)
    grants = ["message_send", "message_status", "message_inbox", "message_reply",
              "wait_register", "wait_get", "task_comment", "agent_message"]
    for profile_id in ("worker-codex", "supervisor"):
        await env.db.create_profile(AgentProfile(
            id=profile_id, name=profile_id, aq_commands=grants,
            harness_tools=[], plugin_tools=[],
        ))
    for project_id in ("p", "q", None):
        await env.db.create_session(SessionRecord(
            id=f"sup-{project_id or 'global'}", project_id=project_id, profile_id="supervisor",
            harness="codex", provider="fake", name=f"n-supervisor--{project_id or 'global'}",
            lifecycle="named", work_dir="/tmp", epoch="boot", instance_token="sup-token",
            started_at=1, state="running",
        ))
    config = AppConfig()
    config.messages.enabled = True
    config.security.capability_enforcement = "enforce"
    handler = CommandHandler(SimpleNamespace(
        db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None,
    ), config)
    store = SessionTokenStore(env.db)
    tokens = {}
    for session_id, project_id, elevated, instance in (
        ("s", "p", False, "token"),
        ("sup-p", "p", True, "sup-token"),
        ("sup-q", "q", True, "sup-token"),
        ("sup-global", None, True, "sup-token"),
    ):
        tokens[session_id] = await store.mint(
            session_id=session_id, session_instance_token=instance,
            task_id=None, project_id=project_id, elevated=elevated,
        )
    monkeypatch.setattr(dependencies, "_token_store", store)
    # Router tests own their handler; a prior create_app() may leave a live
    # orchestrator whose handler (and asyncpg pool) belongs to another loop.
    monkeypatch.setattr(dependencies, "_orchestrator", None)
    monkeypatch.setattr(dependencies, "_command_handler", handler)
    monkeypatch.setattr(dependencies, "_require_session_token", False)
    app = FastAPI()
    app.include_router(execute_router)
    app.include_router(message_router)
    app.add_middleware(TokenAuthMiddleware)
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield SimpleNamespace(http=http, tokens=tokens, handler=handler, store=store)


async def command(api, name, args, *, caller="s"):
    if name == "message_send":
        response = await api.http.post(
            "/api/messages/send", json=args,
            headers={"Authorization": f"Bearer {api.tokens[caller]}"},
        )
        body = response.json()
        return {"ok": "error" not in body, "result": body, **(
            {"error": body["error"]} if "error" in body else {}
        )}
    response = await api.http.post(
        "/api/execute", json={"command": name, "args": args},
        headers={"Authorization": f"Bearer {api.tokens[caller]}"},
    )
    return response.json()


async def native_send(api, args):
    """Native dispatch receives only the scope authenticated from the minted token."""
    scope = await api.store.validate(api.tokens["s"])
    assert scope is not None
    args = dict(args)
    error = await check_request_scope("message_send", args, scope, db=api.handler.db)
    if error:
        return {"error": error}
    return await api.handler.execute("message_send", {**args, "_scope": asdict(scope)})


def send_args(**extra):
    return dict(to_kind="session", to_id="supervisor-p", body="please advise",
                thread_id="owned-report", claim_epoch=1, **extra)


async def test_real_cli_default_supervisor_reply_and_owned_wait(api, env, monkeypatch):
    """Only the Click/async loop bridge is substituted; CLIClient sends real HTTP."""
    loop = asyncio.get_running_loop()
    real_client = CLIClient(base_url="http://test", token=api.tokens["s"])
    async with httpx.AsyncClient(
        transport=api.http._transport, base_url="http://test",
        headers={"Authorization": f"Bearer {api.tokens['s']}"},
    ) as http:
        real_client._http = http

        async def execute(name, args):
            return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(
                real_client.execute(name, args), loop,
            ))

        @asynccontextmanager
        async def cli_client(_api_url):
            yield SimpleNamespace(execute=execute)

        monkeypatch.setattr("src.cli.messages._get_client", cli_client)
        monkeypatch.setenv("AQ_SESSION_ID", "foreign-environment")
        result = await asyncio.to_thread(CliRunner().invoke, cli, [
            "--json", "message", "send", "--to", "session:supervisor-p", "--body", "help",
            "--thread-id", "owned-report", "--claim-epoch", "1",
        ])
    assert result.exit_code == 0, result.output
    sent = json.loads(result.output)["data"]["message"]
    assert (sent["from_kind"], sent["from_id"], sent["project_id"]) == ("session", "s", "p")
    stored = await env.db.get_message(sent["id"])
    assert (stored.from_kind, stored.from_id) == ("session", "s")

    inbox = await command(api, "message_inbox", {"to_kind": "session", "to_id": "sup-p"},
                          caller="sup-p")
    assert sent["id"] in [row["id"] for row in inbox["result"]["messages"]]
    registered = await command(api, "wait_register", dict(
        kind="message", ref=sent["thread_id"], after_seq=sent["created_seq"],
        idempotency_key="owned-report", timeout=300, claim_epoch=1,
    ))
    assert registered["ok"], registered
    wait_id = registered["result"]["wait"]["id"]
    assert registered["result"]["wait"]["state"] == "active"
    replied = await command(api, "message_reply", {"message_id": sent["id"], "body": "proceed"},
                            caller="sup-p")
    assert replied["ok"], replied
    reply = replied["result"]["reply"]
    assert (reply["to_kind"], reply["to_id"], reply["project_id"]) == ("session", "s", "p")
    assert (reply["thread_id"], reply["reply_to_id"]) == (sent["thread_id"], sent["id"])
    await env.db.reconcile_agent_waits(now=time.time())
    shown = await command(api, "wait_get", dict(wait_id=wait_id, consume=True, claim_epoch=1))
    assert shown["ok"], shown
    assert shown["result"]["wait"]["state"] == "satisfied"
    assert shown["result"]["wait"]["digest"]["message_id"] == reply["id"]
    status = await command(api, "message_status", {"message_id": reply["id"]})
    assert status["ok"] and status["result"]["message"]["body"] == "proceed"


@pytest.mark.parametrize("explicit", [
    {}, {"from_kind": "session", "from_id": "s"}, {"from_id": "s"},
    {"from_kind": "session"},
])
async def test_native_dispatch_owns_defaults_and_explicit_own_sender(api, explicit):
    result = await native_send(api, send_args(**explicit))
    assert "error" not in result, result
    row = result["message"]
    assert (row["from_kind"], row["from_id"]) == ("session", "s")


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("generated", [False, True])
async def test_request_model_serialization_preserves_server_sender_default(api, env, named, generated):
    send_model, session_model = MessageSendRequest, SessionMessageRequest
    if generated:
        from tests.test_api_client_contract import _import_repo_client

        _import_repo_client()
        from agent_queue_api_client.models.message_send_request import MessageSendRequest as Send
        from agent_queue_api_client.models.session_message_request import SessionMessageRequest as Named

        send_model, session_model = Send, Named
    if named:
        payload = session_model(body="report", claim_epoch=1)
        url = "/api/sessions/supervisor-p/message"
    else:
        payload = send_model(**send_args())
        url = "/api/messages/send"
    response = await api.http.post(
        url, json=payload.to_dict() if generated else payload.model_dump(by_alias=True),
        headers={"Authorization": f"Bearer {api.tokens['s']}"},
    )
    assert response.status_code == 200, response.text
    stored = await env.db.get_message(response.json()["message_id"])
    assert (stored.from_kind, stored.from_id, stored.project_id) == ("session", "s", "p")


@pytest.mark.parametrize("explicit", [
    {"from_kind": "user", "from_id": "cli"},
    {"from_kind": "user", "from_id": "dashboard"},
    {"from_kind": "system", "from_id": "daemon"},
    {"from_kind": "session", "from_id": "sup-p"},
    {"from_kind": "session", "from_id": "supervisor-p"},
    {"from_kind": "session", "from_id": "foreign"},
    {"from_kind": "session", "from_id": "sup-q"},
])
@pytest.mark.parametrize("path", ["native", "/api/messages/send"])
async def test_forged_sender_refused_before_insert(api, env, explicit, path):
    args = send_args(**explicit)
    if path == "native":
        result = await native_send(api, args)
    else:
        response = await api.http.post(
            path, json=args, headers={"Authorization": f"Bearer {api.tokens['s']}"},
        )
        result = response.json()
    assert "out of scope" in result["error"]
    assert await env.db.list_messages(project_id="p") == []


@pytest.mark.parametrize("change", [
    "instance", "stopped", "project", "profile", "desired_stop", "claim", "holder", "agent",
])
async def test_stale_session_or_claim_refused_even_after_principal_cache(api, env, change):
    first = await command(api, "message_send", send_args())
    assert first["ok"], first
    async with env.db._engine.begin() as conn:
        if change in {"instance", "stopped", "project", "profile", "desired_stop"}:
            values = {"instance": {"instance_token": "replaced"},
                      "stopped": {"state": "stopped"}, "project": {"project_id": "q"},
                      "profile": {"profile_id": "supervisor"},
                      "desired_stop": {"desired_state": "stopped"}}[change]
            await conn.execute(update(sessions).where(sessions.c.id == "s").values(**values))
        elif change == "agent":
            await conn.execute(update(agents).where(agents.c.id == "a").values(current_task_id=None))
        else:
            values = {"claim_epoch": 2} if change == "claim" else {"assigned_agent_id": None}
            await conn.execute(update(tasks).where(tasks.c.id == "owner").values(**values))
    result = await command(api, "message_send", send_args())
    assert not result["ok"] and "out of scope" in result["error"], result
    assert len(await env.db.list_messages(project_id="p")) == 1


@pytest.mark.parametrize("lifecycle", ["task", "named"])
async def test_ordinary_task_and_named_machine_session_defaults(api, env, lifecycle):
    async with env.db._engine.begin() as conn:
        await conn.execute(update(sessions).where(sessions.c.id == "s").values(
            lifecycle=lifecycle, **({"task_id": None, "agent_id": None} if lifecycle == "named"
                                     else {}),
        ))
    args = send_args()
    args.pop("claim_epoch")
    result = await command(api, "message_send", args)
    assert result["ok"], result
    assert (result["result"]["message"]["from_kind"], result["result"]["message"]["from_id"]) == (
        "session", "s",
    )


async def test_pool_epoch_and_legacy_user_thread_ownership_remain_required(api):
    args = send_args()
    args.pop("claim_epoch")
    result = await command(api, "message_send", args)
    assert not result["ok"] and "live claim" in result["error"]
    legacy = await api.http.post("/api/messages/send", json=send_args(project_id="p"))
    message = legacy.json()["message"]
    assert (message["from_kind"], message["from_id"]) == ("user", "cli")
    refused = await command(api, "wait_register", dict(
        kind="message", ref=message["thread_id"], after_seq=message["created_seq"],
        claim_epoch=1, timeout=300, idempotency_key="legacy",
    ))
    assert not refused["ok"] and "not authorized" in refused["error"]
    foreign = await command(api, "message_status", {"message_id": message["id"]}, caller="sup-q")
    assert not foreign["ok"] and "not found" in foreign["error"]


async def test_foreign_project_and_server_identity_args_do_not_change_ownership(api):
    foreign = await command(api, "message_send", send_args(project_id="q"))
    assert not foreign["ok"] and "project_id mismatch" in foreign["error"]
    result = await command(api, "message_send", send_args(
        _scope={"kind": "session", "session_id": "sup-p", "elevated": True},
        _principal={"kind": "local"},
    ))
    assert result["ok"], result
    assert result["result"]["message"]["from_id"] == "s"


@pytest.mark.parametrize("address", ["sup-p", "supervisor-p", "n-supervisor--p"])
async def test_live_supervisor_may_use_its_owned_explicit_address(api, address):
    result = await command(api, "message_send", dict(
        to_kind="session", to_id="s", body="guidance", from_kind="session", from_id=address,
    ), caller="sup-p")
    assert result["ok"], result
    assert result["result"]["message"]["from_id"] == address
    forged = await command(api, "message_send", dict(
        to_kind="session", to_id="s", body="guidance", from_kind="session", from_id="supervisor-q",
    ), caller="sup-p")
    assert not forged["ok"] and "out of scope" in forged["error"]


async def test_local_human_defaults_and_explicit_senders_remain_distinct(api):
    for explicit, expected in (
        ({}, ("user", "cli")),
        ({"from_kind": "user", "from_id": "discord:42"}, ("user", "discord:42")),
        ({"from_kind": "session", "from_id": "supervisor-p"}, ("session", "supervisor-p")),
        ({"from_kind": "system", "from_id": "notice"}, ("system", "notice")),
    ):
        response = await api.http.post("/api/messages/send",
                                       json=send_args(project_id="p", **explicit))
        result = response.json()
        assert "error" not in result, result
        row = result["message"]
        assert (row["from_kind"], row["from_id"]) == expected


async def test_global_supervisor_system_notice_remains_nonreplyable(api):
    result = await command(api, "message_send", dict(
        to_kind="session", to_id="s", body="notice", from_kind="system", from_id="maintenance",
    ), caller="sup-global")
    assert result["ok"], result
    row = result["result"]["message"]
    assert row["from_kind"] == "system"
    reply = await command(api, "message_reply", {"message_id": row["id"], "body": "ack"})
    assert not reply["ok"] and "cannot reply to a 'system' message" in reply["error"]


@pytest.mark.parametrize("notification", ["task_comment", "agent_message"])
async def test_authenticated_supervisor_notices_preserve_native_sender_semantics(api, env, notification):
    args = ({"task_id": "owner", "body": "check the finding"}
            if notification == "task_comment" else {"target": "owner", "body": "guidance"})
    result = await command(api, notification, args, caller="sup-p")
    assert result["ok"], result
    [stored] = await env.db.list_messages(project_id="p")
    expected = (("system", "task-comment:supervisor:sup-p")
                if notification == "task_comment" else ("session", "supervisor-p"))
    assert (stored.from_kind, stored.from_id) == expected
    reply = await command(api, "message_reply", {"message_id": stored.id, "body": "ack"})
    if notification == "task_comment":
        assert not reply["ok"] and "cannot reply to a 'system' message" in reply["error"]
    else:
        assert reply["ok"], reply
        assert reply["result"]["reply"]["from_id"] == "s"
        assert reply["result"]["reply"]["to_id"] == "supervisor-p"
    # The same caller cannot choose a system sender on ordinary message_send.
    forged = await command(api, "message_send", dict(
        to_kind="session", to_id="s", body="forged", from_kind="system", from_id=stored.from_id,
    ), caller="sup-p")
    assert not forged["ok"] and "out of scope" in forged["error"]
    assert len(await env.db.list_messages(project_id="p")) == (
        1 if notification == "task_comment" else 2
    )
