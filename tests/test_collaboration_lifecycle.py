"""Bounded expiry, retention and daemon-only collaboration reconciliation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, insert, select

from src.agent_waits import ProducerObservation
from src.collaboration import (
    CollaborationError,
    CONTENT_RETENTION_SECONDS,
    TOMBSTONE_RETENTION_SECONDS,
)
from src.commands import CommandHandler
from src.commands.collaboration_lifecycle import CollaborationReconciler
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    current_principal,
    principal_context,
)
from src.config import AppConfig
from src.database.tables import (
    collaboration_members,
    collaboration_messages,
    collaboration_threads,
    messages,
)
from src.orchestrator import Orchestrator
from src.profiles.capabilities import DENY_ALL
from tests.test_collaboration_queries import NOW, accept, count, create, env as env, send


@pytest.fixture
def commands(env):
    return CommandHandler(
        SimpleNamespace(db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None),
        AppConfig(),
    )


async def close(env, thread, *, now=NOW + 2):
    return await env.db.close_collaboration_thread(
        thread_id=thread["id"], project_id="p", reason="closed", now=now
    )


async def test_expiry_final_result_and_notifications_are_once(commands, env):
    thread = await create(env, deadline_seconds=60)
    tick = CollaborationReconciler(commands).tick
    assert (await tick(now=NOW + 59))["expired"] == 0
    first, second = await asyncio.gather(tick(now=NOW + 60), tick(now=NOW + 60))
    assert first["success"] and second["success"]
    assert first["expired"] + second["expired"] == 1
    current = await env.db.get_collaboration_thread(thread["id"], project_id="p")
    assert current["state"] == "expired" and current["closed_at"] == NOW + 60
    assert current["final_result"] == dict(reason="expired", note=None, message_count=0, last_seq=0)
    assert await count(env, thread, "collaboration_closed") == 2
    assert (await tick(now=NOW + 61))["expired"] == 0
    assert await count(env, thread, "collaboration_closed") == 2


async def test_retention_keeps_tombstones_then_cascades(commands, env):
    thread = await create(env, task_ids=("one", "two", "three"))
    await accept(env, thread)
    first = await send(env, thread)
    # Reply links must not stop retention deleting the original body.
    await send(env, thread, key="reply", reply_to_id=first["message_id"])
    closed = await close(env, thread)
    tick = CollaborationReconciler(commands).tick
    before = await tick(now=closed["closed_at"] + CONTENT_RETENTION_SECONDS)
    assert before["messages_deleted"] == 0
    retained = await tick(now=closed["closed_at"] + 31 * 86400)
    assert retained["messages_deleted"] == 10
    for kind in ("collaboration", "collaboration_invite", "collaboration_closed"):
        assert await count(env, thread, kind) == 0
    async with env.db._engine.connect() as conn:
        links = (await conn.execute(select(collaboration_messages))).mappings().all()
    assert len(links) == 2 and all(link["message_id"] is None for link in links)
    assert [link["client_key"] for link in links] == ["send", "reply"]
    assert (await env.db.get_collaboration_thread(thread["id"], project_id="p"))[
        "final_result"
    ] == closed["final_result"]
    with pytest.raises(CollaborationError) as error:
        await send(env, thread, now=closed["closed_at"] + 31 * 86400)
    assert error.value.code == "collaboration.closed"
    boundary = await tick(now=closed["closed_at"] + TOMBSTONE_RETENTION_SECONDS)
    assert boundary["threads_deleted"] == 0
    deleted = await tick(now=closed["closed_at"] + 91 * 86400)
    assert deleted["threads_deleted"] == 1
    assert await env.db.get_collaboration_thread(thread["id"], project_id="p") is None
    async with env.db._engine.connect() as conn:
        for table in (collaboration_members, collaboration_messages):
            assert await conn.scalar(select(func.count()).select_from(table)) == 0


async def test_expiry_batch_is_bounded_and_deadline_ordered(commands, env):
    rows = []
    for index in range(105):
        thread = await create(env, key=f"due-{index}", deadline_seconds=60 + index)
        rows.append(thread)
    result = await CollaborationReconciler(commands).tick(now=NOW + 200)
    assert result["expired"] == 100
    async with env.db._engine.connect() as conn:
        active = (
            (
                await conn.execute(
                    select(collaboration_threads.c.id).where(
                        collaboration_threads.c.state == "active"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert set(active) == {thread["id"] for thread in rows[100:]}
    assert (await CollaborationReconciler(commands).tick(now=NOW + 200))["expired"] == 5


async def test_retention_batches_and_unrelated_messages(commands, env):
    thread = await create(env)
    await close(env, thread)
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(messages),
            [
                dict(
                    id=f"body-{i}",
                    project_id="p",
                    from_kind="system",
                    from_id="test",
                    to_kind="task",
                    to_id="one",
                    thread_id=thread["id"],
                    body="body",
                    body_kind="collaboration",
                    created_at=NOW,
                )
                for i in range(501)
            ],
        )
        await conn.execute(
            insert(messages).values(
                id="unrelated",
                project_id="p",
                from_kind="system",
                from_id="test",
                to_kind="task",
                to_id="one",
                thread_id=thread["id"],
                body="Keep me",
                body_kind="ordinary",
                created_at=NOW,
            )
        )
    tick = CollaborationReconciler(commands).tick
    first = await tick(now=NOW + 91 * 86400)
    assert first["messages_deleted"] == 500 and first["threads_deleted"] == 0
    assert await env.db.get_collaboration_thread(thread["id"], project_id="p") is not None
    second = await tick(now=NOW + 91 * 86400)
    assert second["messages_deleted"] == 5 and second["threads_deleted"] == 1
    async with env.db._engine.connect() as conn:
        assert (
            await conn.scalar(select(messages.c.body).where(messages.c.id == "unrelated"))
            == "Keep me"
        )


async def test_thread_deletion_is_bounded(commands, env):
    old = NOW - 91 * 86400
    async with env.db._engine.begin() as conn:
        await conn.execute(
            insert(collaboration_threads),
            [
                dict(
                    id=f"collab-old-{i}",
                    project_id="p",
                    created_by_kind="operator",
                    created_by_id="operator",
                    idempotency_key=f"old-{i}",
                    request_hash="hash",
                    state="closed",
                    created_at=old - 60,
                    deadline_at=old,
                    closed_at=old,
                    close_reason="closed",
                )
                for i in range(505)
            ],
        )
    result = await CollaborationReconciler(commands).tick(now=NOW)
    assert result["threads_deleted"] == 500
    assert (await CollaborationReconciler(commands).tick(now=NOW))["threads_deleted"] == 5


@pytest.mark.parametrize(
    "kind", [None, PrincipalKind.LOCAL, PrincipalKind.SESSION, PrincipalKind.PLAYBOOK]
)
async def test_only_service_principal_may_reconcile(kind):
    from src.commands.collaboration_lifecycle import CollaborationLifecycleMixin

    handler = CollaborationLifecycleMixin()
    handler.db = SimpleNamespace(reconcile_collaborations=AsyncMock())
    if kind is None:
        result = await handler._cmd_reconcile_collaborations({})
    else:
        with principal_context(ExecutionPrincipal(kind=kind, policy=DENY_ALL, elevated=True)):
            result = await handler._cmd_reconcile_collaborations({})
    assert not result["success"] and result["error_code"] == "collaboration.out_of_scope"
    handler.db.reconcile_collaborations.assert_not_awaited()


async def test_reconciler_supplies_service_identity_and_default_clock(monkeypatch):
    handler = SimpleNamespace(execute=AsyncMock())

    async def execute(name, args):
        assert name == "reconcile_collaborations"
        assert current_principal().kind == PrincipalKind.SERVICE
        return {"success": True}

    handler.execute.side_effect = execute
    assert (await CollaborationReconciler(handler).tick())["success"]
    from src.commands.collaboration_lifecycle import CollaborationLifecycleMixin

    mixin = CollaborationLifecycleMixin()
    mixin.db = SimpleNamespace(reconcile_collaborations=AsyncMock(return_value={"expired": 0}))
    monkeypatch.setattr("src.commands.collaboration_lifecycle.time.time", lambda: NOW)
    with principal_context(ExecutionPrincipal.service("test")):
        assert (await mixin._cmd_reconcile_collaborations({}))["success"]
    mixin.db.reconcile_collaborations.assert_awaited_once_with(now=NOW)


def test_command_is_internal_with_fallback_schema():
    from src.api.codegen import API_EXCLUDED
    from src.mcp_registration import DEFAULT_EXCLUDED_COMMANDS, effective_tool_definitions
    from src.tools.definitions import _FALLBACK_INPUT_SCHEMAS

    assert "reconcile_collaborations" in API_EXCLUDED
    assert "reconcile_collaborations" in DEFAULT_EXCLUDED_COMMANDS
    assert "now" in _FALLBACK_INPUT_SCHEMAS["reconcile_collaborations"]["properties"]
    assert "reconcile_collaborations" not in {tool["name"] for tool in effective_tool_definitions()}


@pytest.mark.parametrize("failure", [None, "refused", "exception"])
async def test_orchestrator_expires_before_waits_even_on_failure(monkeypatch, failure):
    from src.agent_waits import AgentWaitReconciler

    seen = []

    async def expire(self):
        seen.append("collaboration")
        if failure == "exception":
            raise RuntimeError("temporary failure")
        return {"success": failure != "refused"}

    async def waits(self):
        seen.append("waits")
        return {"success": True}

    monkeypatch.setattr(CollaborationReconciler, "tick", expire)
    monkeypatch.setattr(AgentWaitReconciler, "tick", waits)
    monkeypatch.setattr(
        "src.integration.completion_recovery.schedule_ready_owner_recovery", lambda _: None
    )
    orch = SimpleNamespace(
        config=AppConfig(),
        _command_handler=object(),
        transcript_watcher=SimpleNamespace(tick=AsyncMock()),
        agent_questions=SimpleNamespace(tick=AsyncMock()),
        session_reconciler=SimpleNamespace(tick=AsyncMock()),
    )
    orch.config.sessions.enabled = True
    await Orchestrator._reconcile_sessions(orch)
    assert seen == ["collaboration", "waits"]
    orch.session_reconciler.tick.assert_awaited_once()


@pytest.mark.skipif(
    "reason" not in ProducerObservation.__dataclass_fields__,
    reason="Task 3 collaboration wait semantics have not landed yet",
)
async def test_expiry_resolves_collaboration_wait_in_same_cycle(commands, env):
    from src.agent_waits import AgentWaitReconciler

    thread = await create(env, deadline_seconds=60)
    await accept(env, thread)
    wait = await env.db.register_agent_wait(
        identity=dict(
            session_id="s-one",
            instance_token="token-one",
            project_id="p",
            claim_epoch=1,
            elevated=False,
        ),
        kind="message",
        match={"thread_id": thread["id"], "after_seq": 0},
        deadline_at=NOW + 600,
        idempotency_key="expiry",
        now=NOW,
    )
    assert wait["state"] == "active"
    assert (await CollaborationReconciler(commands).tick(now=NOW + 60))["expired"] == 1
    assert (await AgentWaitReconciler(commands).tick(now=NOW + 60))["success"]
    result = await env.db.get_agent_wait(wait["id"])
    assert result["state"] == "satisfied" and result["result_digest"]["reason"] == "thread_closed"
