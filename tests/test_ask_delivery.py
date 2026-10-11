"""Canonical root writes survive late HTTP completion, restart, pause and repair."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from src.asks.delivery import AskDeliveryAdapter, config_fingerprint, destination
from src.asks.intake import input_values
from src.asks.render import render
from src.commands.principal import ExecutionPrincipal, principal_context
from src.database.tables import ask_external_bindings, gates, human_communication_modes, projects
from src.delivery.scheduler import DeliveryScheduler
from src.digest.dispatch import DigestScheduleService
from src.escalations.transport import SinkTransport, TransportAmbiguous, _units
from src.remote_links import DashboardLink
from tests.test_ask_sources import _human_gate, _source_preview


@pytest.fixture(name="delivery_env")
async def delivery_env(command_handler_factory):
    handler = await command_handler_factory()
    handler.config.discord.channel_id = "2"
    handler.config.discord.authorized_users = ["3"]
    gate, ask = await _human_gate(handler)
    now = [time.time()]
    sink = SinkTransport()
    links = SimpleNamespace(resolve=AsyncMock(return_value=DashboardLink(url="https://aq.example")))
    adapter = AskDeliveryAdapter(
        handler, sink, lease_owner="ask-worker", links=links, clock=lambda: now[0]
    )
    handler.ask_delivery = adapter
    async with handler.db.immediate() as conn:
        await handler.db.advance_ask_mode(
            {
                "project_id": ask["project_id"],
                "destination": destination(handler.config),
                "mode": "channel",
                "config_fingerprint": config_fingerprint(handler.config),
                "updated_at": now[0],
            },
            expected_generation=0,
            conn=conn,
        )
    env = SimpleNamespace(
        handler=handler,
        db=handler.db,
        adapter=adapter,
        sink=sink,
        now=now,
        gate=gate,
        ask=ask,
        links=links,
    )
    yield env
    await handler.db.close()


async def claim(env):
    rows = await env.db.claim_report_deliveries(
        lease_owner="ask-worker", now=env.now[0], include_digest=False, limit=1
    )
    return rows[0] if rows else None


async def settle(env, limit=10):
    await env.adapter.reconcile()
    for _ in range(limit):
        row = await claim(env)
        if not row:
            break
        await env.adapter.deliver(row)


async def binding(env):
    async with env.db.immediate() as conn:
        return await env.db.active_ask_binding(
            env.ask["id"], destination(env.handler.config), conn=conn
        )


async def change_title(env, title):
    async with env.db.immediate() as conn:
        await conn.execute(update(gates).where(gates.c.id == env.gate["id"]).values(title=title))


async def test_one_root_buttons_and_latest_render(delivery_env):
    env = delivery_env
    await settle(env)
    original = await binding(env)
    assert original["root_state"] == "confirmed"
    assert len(env.sink.messages) == 1
    assert len(env.sink.messages[original["external_root_id"]].buttons) == 2
    await change_title(env, "Newest question")
    await settle(env)
    assert (await binding(env))["external_root_id"] == original["external_root_id"]
    assert "Newest question" in env.sink.messages[original["external_root_id"]].content
    assert not env.sink.threads


async def test_pause_before_claim_and_immediately_before_send_consumes_no_attempt(delivery_env):
    env = delivery_env
    ident = await env.adapter.enqueue(env.ask["id"])
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == env.ask["project_id"]).values(status="PAUSED")
        )
    assert await claim(env) is None
    assert (await env.db.get_outbound_delivery(ident))["attempt_count"] == 0
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == env.ask["project_id"]).values(status="ACTIVE")
        )
    row = await claim(env)
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == env.ask["project_id"]).values(status="PAUSED")
        )
    await env.adapter.deliver(row)
    assert not env.sink.messages
    assert (await env.db.get_outbound_delivery(ident))["attempt_count"] == 0
    async with env.db.immediate() as conn:
        await conn.execute(
            update(projects).where(projects.c.id == env.ask["project_id"]).values(status="ACTIVE")
        )
    env.now[0] += 31
    await settle(env)
    assert len(env.sink.messages) == 1


async def test_legacy_alias_returns_canonical_delivery_receipt(delivery_env):
    env = delivery_env
    submitted = await env.handler.execute(
        "review_submit",
        {
            "project_id": env.ask["project_id"],
            "kind": "spec",
            "title": "Delivery alias review",
            "content": "# Design\n\nReview the proposed delivery behavior.\n",
        },
    )
    assert submitted["success"], submitted
    shown = await env.handler.execute("review_show", {"review_id": submitted["review_id"]})
    assert shown["success"], shown
    env.ask = shown["ask"]
    await settle(env)
    root = await binding(env)
    canonical = await env.handler.execute("ask_show", {"ask_id": env.ask["id"]})
    alias = await env.handler.execute(
        "ask_show", {"ask_id": submitted["review_id"], "alias_kind": "review"}
    )

    assert canonical["success"] and alias["success"], (canonical, alias)
    assert alias["ask"]["id"] == canonical["ask"]["id"] == env.ask["id"]
    assert alias["ask"]["delivery"] == canonical["ask"]["delivery"]
    assert alias["ask"]["delivery"]["status"] == "Confirmed"
    assert alias["ask"]["delivery"]["bindings"][0]["external_root_id"] == root["external_root_id"]


async def test_unknown_create_survives_restart_without_repost(delivery_env):
    env = delivery_env
    env.sink.faults.append(("post_root", TransportAmbiguous("receipt lost")))
    ident = await env.adapter.enqueue(env.ask["id"])
    await env.adapter.deliver(await claim(env))
    assert (await env.db.get_outbound_delivery(ident))["state"] == "unknown"
    shown = await env.handler._cmd_ask_show({"ask_id": env.ask["id"]})
    assert shown["ask"]["delivery"]["status"] == "Delivery unknown"
    env.now[0] += 1000
    env.adapter = AskDeliveryAdapter(
        env.handler, env.sink, lease_owner="ask-worker", links=env.links, clock=lambda: env.now[0]
    )
    await settle(env)
    assert env.sink.calls.count("post_root") == 1
    row = await env.db.get_outbound_delivery(ident)
    late = env.sink.record("2", row["payload"]["text"] + "\n" + row["marker"])
    await settle(env)
    assert (await binding(env))["external_root_id"] == late.id
    assert len(env.sink.messages) == 1
    shown = await env.handler._cmd_ask_show({"ask_id": env.ask["id"]})
    assert shown["ask"]["delivery"]["repairs"] == []


async def test_expired_lease_cannot_overtake_late_old_edit(delivery_env):
    env = delivery_env
    await settle(env)
    await change_title(env, "Old in-flight render")
    await env.adapter.enqueue(env.ask["id"])
    row = await claim(env)
    entered, release = asyncio.Event(), asyncio.Event()
    edit = env.sink.edit_root

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        await edit(**kwargs)

    env.sink.edit_root = delayed
    task = asyncio.create_task(env.adapter.deliver(row))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        await change_title(env, "Latest desired render")
        env.now[0] += 121
        await env.adapter.reconcile()
        assert await claim(env) is None
        assert (await binding(env))["inflight_id"] == row["id"]
    finally:
        release.set()
        await task
    env.sink.edit_root = edit
    await settle(env)
    root = (await binding(env))["external_root_id"]
    assert "Latest desired render" in env.sink.messages[root].content
    assert (await env.db.get_outbound_delivery(row["id"]))["state"] == "sent"


async def test_deleted_root_replaces_once_and_retires_tokens(delivery_env):
    env = delivery_env
    await settle(env)
    old = await binding(env)
    env.sink.delete(old["external_root_id"])
    await change_title(env, "Refresh deleted card")
    await settle(env)
    new = await binding(env)
    assert new["root_generation"] == 2
    assert new["external_root_id"] != old["external_root_id"]
    assert len(env.sink.messages) == 1
    assert new["alternate_roots"][0]["receipt"] == old["receipt"]
    await settle(env)
    assert (await binding(env))["root_generation"] == 2


async def test_repair_and_late_duplicate_redirect(delivery_env):
    env = delivery_env
    env.sink.faults.append(("post_root", TransportAmbiguous("unknown create")))
    ident = await env.adapter.enqueue(env.ask["id"])
    await env.adapter.deliver(await claim(env))
    refusal = await env.handler._cmd_ask_repair_delivery(
        {
            "delivery_id": ident,
            "expected_generation": 1,
            "disposition": "confirmed_absent",
            "evidence": "History marker absent",
        }
    )
    assert refusal["error_code"] == "ask.delivery_unknown"
    result = await env.handler._cmd_ask_repair_delivery(
        {
            "delivery_id": ident,
            "expected_generation": 1,
            "disposition": "cancel",
            "evidence": "Operator explicitly accepts irreconcilable HTTP completion",
        }
    )
    assert result["success"], result
    await settle(env)
    current = await binding(env)
    row = await env.db.get_outbound_delivery(ident)
    duplicate = env.sink.record("2", row["payload"]["text"] + "\n" + row["marker"])
    await settle(env)
    assert (await binding(env))["external_root_id"] == current["external_root_id"]
    assert "This request moved" in env.sink.messages[duplicate.id].content
    assert not env.sink.messages[duplicate.id].buttons
    assert (await env.db.get_outbound_delivery(ident))["external_receipt_id"] == duplicate.id


@pytest.mark.parametrize("change", ["allowlist", "mode", "audience"])
async def test_privacy_or_transfer_change_between_claim_and_send(delivery_env, change):
    env = delivery_env
    await env.adapter.enqueue(env.ask["id"])
    row = await claim(env)
    if change == "allowlist":
        env.handler.config.discord.authorized_users = ["different-human"]
    elif change == "mode":
        async with env.db.immediate() as conn:
            await conn.execute(
                update(human_communication_modes).values(mode="transferring", generation=2)
            )
    else:
        env.handler.config.discord.project_id = "some-other-project"
    await env.adapter.deliver(row)
    assert not env.sink.messages
    assert (await env.db.get_outbound_delivery(row["id"]))["state"] == "cancelled"


async def test_missing_external_origin_is_visible_repair_case(delivery_env):
    env = delivery_env
    env.links.resolve.return_value = DashboardLink()
    assert await env.adapter.enqueue(env.ask["id"]) is None
    async with env.db.immediate() as conn:
        from src.database.tables import ask_reconciliation_cases

        case = (await conn.execute(select(ask_reconciliation_cases))).mappings().one()
    assert "Remote dashboard origin" in case["reason"]
    shown = await env.handler._cmd_ask_show({"ask_id": env.ask["id"]})
    assert shown["ask"]["delivery"]["status"] == "Needs attention"
    assert not env.sink.messages


async def test_shared_scheduler_delivers_with_digest_disabled_and_routine_fairness(delivery_env):
    env = delivery_env
    digest = DigestScheduleService(
        env.db, env.sink, config=env.handler.config, lease_owner="ask-worker", pump_enabled=False
    )
    scheduler = DeliveryScheduler(
        env.db,
        env.sink,
        config=env.handler.config,
        lease_owner="ask-worker",
        digest=digest,
        ask_adapter=env.adapter,
        clock=lambda: env.now[0],
    )
    for n in range(6):
        await env.db.reserve_outbound_delivery(
            owner_kind="conversation",
            owner_id=f"c{n}",
            dedup_key=f"conversation-{n}",
            destination={"transport": "discord", "channel_id": "2"},
            payload={"text": f"Priority reply {n}"},
            due_at=1,
            now=1,
        )
    # A test adapter uses the same transport while preserving the scheduler's real claim query.
    from src.delivery.dispatch import OutboundAdapter

    scheduler.conversation = OutboundAdapter(env.db, scheduler.delivery, lease_owner="ask-worker")
    report, _ = await env.db.reserve_outbound_delivery(
        owner_kind="morning",
        owner_id="r",
        dedup_key="routine",
        destination={"transport": "discord", "channel_id": "2"},
        payload={"text": "Routine report"},
        due_at=2,
        now=2,
    )
    await scheduler.tick(limit=4)
    assert (await env.db.get_outbound_delivery(report["id"]))["state"] == "sent"
    assert list(env.sink.messages.values())[3].content.startswith("Routine report")
    await scheduler.tick(limit=10)
    assert (await binding(env))["root_state"] == "confirmed"


async def test_unique_canonical_binding(delivery_env):
    env = delivery_env
    await asyncio.gather(*(env.adapter.enqueue(env.ask["id"]) for _ in range(3)))
    async with env.db.immediate() as conn:
        rows = (await conn.execute(select(ask_external_bindings))).all()
    assert len(rows) == 1


async def test_reply_is_versioned_rebased_and_uses_native_reference(delivery_env):
    env = delivery_env
    await settle(env)
    values = input_values(
        transport="discord",
        external_id="human-message",
        actor="human:discord:3",
        author_id="3",
        channel_id="2",
        guild_id="1",
        text="Explain",
        source="gateway",
        project_id=env.ask["project_id"],
    )
    async with env.db.immediate() as conn:
        item, _, _ = await env.db.admit_ask_input(values, conn=conn)
        await env.adapter.reserve_reply(
            env.ask, item, "Complete answer", entry_id="answer-1", conn=conn
        )
    await change_title(env, "New display while answer was queued")
    await settle(env)
    replies = [m for m in env.sink.messages.values() if m.content.startswith("Complete answer")]
    assert len(replies) == 1
    assert replies[0].reference_message_id == "human-message"
    assert not env.sink.threads


async def test_gateway_discussion_ack_uses_shared_ask_ledger(delivery_env):
    env = delivery_env
    env.handler.config.discord.guild_id = "1" * 18
    env.handler.config.discord.channel_id = "2" * 18
    env.handler.config.discord.authorized_users = ["3" * 18]
    async with env.db.immediate() as conn:
        await conn.execute(
            update(human_communication_modes).values(
                destination=destination(env.handler.config),
                config_fingerprint=config_fingerprint(env.handler.config),
            )
        )
    await settle(env)
    with principal_context(ExecutionPrincipal.service("discord-gateway")):
        result = await env.handler._cmd_ask_intake(
            {
                "source": "gateway",
                "envelope": {
                    "transport": "discord",
                    "guild_id": "1" * 18,
                    "channel_id": "2" * 18,
                    "external_message_id": "4" * 18,
                    "external_root_message_id": "4" * 18,
                    "author_id": "3" * 18,
                    "text": f"Please explain {env.ask['id']}",
                    "received_at": env.now[0],
                    "mentions_bot": False,
                },
            }
        )
    assert result["success"], result
    rows = await env.db.list_outbound_deliveries(owner_kind="ask", owner_id=env.ask["id"])
    assert any(r["send_kind"] == "reply" for r in rows), rows
    await settle(env)
    assert any(m.reference_message_id == "4" * 18 for m in env.sink.messages.values())


async def test_global_config_fence_survives_yaml_reversion(delivery_env):
    env = delivery_env
    await settle(env)
    env.handler.config.discord.authorized_users = ["someone-else"]
    await env.adapter.reconcile()
    env.handler.config.discord.authorized_users = ["3"]
    await env.adapter.reconcile()
    async with env.db.immediate() as conn:
        mode = await env.db.get_ask_mode(
            env.ask["project_id"], destination(env.handler.config), conn=conn
        )
    assert mode["mode"] == "delivery_disabled" and mode["generation"] == 3
    result = await env.handler._cmd_ask_cutover(
        {"project_id": env.ask["project_id"], "mode": "legacy", "expected_generation": 3}
    )
    assert result["error_code"] == "ask.legacy_retired", result


async def test_cutover_refuses_unverified_inventory_or_outstanding_http(delivery_env):
    env = delivery_env
    result = await env.handler._cmd_ask_cutover(
        {"project_id": env.ask["project_id"], "mode": "channel", "expected_generation": 1}
    )
    assert result["error_code"] == "ask.transfer_unverified", result
    env.sink.faults.append(("post_root", TransportAmbiguous("Unresolved HTTP")))
    await settle(env)
    result = await env.handler._cmd_ask_cutover(
        {"project_id": env.ask["project_id"], "mode": "transferring", "expected_generation": 1}
    )
    assert result["success"] and result["result"]["mode"] == "transferring", result
    assert (await binding(env))["inflight_id"] is not None
    blocked = await env.handler._cmd_ask_cutover({
        "project_id": env.ask["project_id"], "mode": "channel", "expected_generation": 2,
    })
    assert blocked["error_code"] == "ask.transfer_unverified", blocked
    rollback = await env.handler._cmd_ask_cutover({
        "project_id": env.ask["project_id"], "mode": "delivery_disabled", "expected_generation": 2,
    })
    assert rollback["success"] and rollback["result"]["generation"] == 3
    assert (await binding(env))["inflight_id"] is not None
    await settle(env)
    assert env.sink.calls.count("post_root") == 1


async def test_render_long_or_document_meaning_never_has_partial_approval_controls(delivery_env):
    env = delivery_env
    ask = {**env.ask, "source_kind": "review"}
    card = render(ask, base_url="https://aq.example")
    assert "Read and discuss" in card.text and not card.choices
    ask = {**env.ask, "context": {**env.ask["context"], "title": "@everyone " + "😀" * 3000}}
    card = render(ask, base_url="https://aq.example")
    assert not card.choices
    assert _units(card.text) < 1000
    assert "https://aq.example" in card.text


async def test_late_pending_edit_cannot_leave_applied_outcome_actionable(delivery_env):
    env = delivery_env
    await settle(env)
    await change_title(env, "Decision still pending")
    await env.adapter.enqueue(env.ask["id"])
    entered, release = asyncio.Event(), asyncio.Event()
    edit = env.sink.edit_root

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        await edit(**kwargs)

    env.sink.edit_root = delayed
    task = asyncio.create_task(env.adapter.deliver(await claim(env)))
    await asyncio.wait_for(entered.wait(), 5)
    try:
        shown = await env.handler._cmd_gate_show({"gate_id": env.gate["id"]})
        args = await _source_preview(env.handler, shown["gate"]["ask"], "approve")
        result = await env.handler._cmd_ask_decide(args)
        assert result["success"], result
        env.now[0] += 121
        await env.adapter.reconcile()
        assert await claim(env) is None
    finally:
        release.set()
        await task
    env.sink.edit_root = edit
    await settle(env)
    card = env.sink.messages[(await binding(env))["external_root_id"]]
    assert not card.buttons and "applied" in card.content


async def test_bounded_reconciliation_rotates_past_broken_sources(delivery_env):
    from src.asks.models import AskError

    env = delivery_env
    result = await env.handler.execute(
        "gate_create",
        {
            "project_id": env.ask["project_id"],
            "gate_type": "human",
            "title": "Second gate",
            "await_id": "second-test-gate",
            "question": "Proceed?",
            "human_request": {
                "policy": "approval_hold_v1",
                "context": {"waiting_effect": "Waiting for authorization"},
            },
        },
    )
    assert result["success"], result
    shown = await env.handler._cmd_gate_show({"gate_id": result["gate_id"]})
    assert shown['gate']['ask']['id'] != env.ask['id']
    seen = []

    async def unavailable(ask_id):
        seen.append(ask_id)
        raise AskError("ask.not_found", "Source unavailable")

    env.adapter.enqueue = unavailable
    await env.adapter.reconcile(limit=1)
    await env.adapter.reconcile(limit=1)
    assert len(set(seen)) == 2
