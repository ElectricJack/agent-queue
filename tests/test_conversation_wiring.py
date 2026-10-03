"""Production outbox binding and project-supervisor conversation round trips."""

from __future__ import annotations

import os
import signal
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import src.main as main_mod
import tests.test_main_lifecycle as lifecycle_tests
import tests.test_supervisor_inbox_commands as inbox_tests
import tests.test_tmux_integration as tmux_tests
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import load_config
from src.conversations.outbox import UnboundOutbox
from src.conversations.preconditions import conversation_preconditions
from src.digest import DigestScheduleService
from src.discord.inbound import DiscordInboundRouter
from src.discord.intake_diagnostics import IgnoreCounter
from src.discord.rate_guard import OutboundTokenBucket
from src.escalations.transport import (
    SinkMessage,
    SinkTransport,
    TransportAmbiguous,
    TransportUnavailable,
)
from src.messages.delivery import MessageDeliveryEngine
from src.messages.session_lens import SessionLens
from src.models import Project, SessionRecord
from src.profiles.capabilities import CapabilityPolicy
from tests.test_main_lifecycle import (
    _FakeAdapter,
    _install_run_env,
    _postgres_config,
)
from tests.test_supervisor_inbox_commands import (
    AUTHOR,
    BOT,
    CHANNEL,
    GUILD,
    NOW,
    post,
    reply,
    reply_args,
)
from tests.test_tmux_integration import (
    _received,
    _spec,
)


env = inbox_tests.env
lifecycle_clock = lifecycle_tests.lifecycle_clock
provider = tmux_tests.provider
stub_path = tmux_tests.stub_path


async def supervisor(db, project_id=None, *, state="running", token="live-token", work_dir="/tmp"):
    address = f"supervisor-{project_id or 'global'}"
    row = SessionRecord(
        id=f"launch-{address}",
        project_id=project_id,
        profile_id="supervisor",
        harness="codex",
        provider="tmux",
        name=f"n-supervisor--{project_id or 'global'}",
        lifecycle="named",
        work_dir=work_dir,
        epoch="test",
        instance_token=token,
        started_at=NOW,
        state=state,
    )
    await db.create_session(row)
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=CapabilityPolicy.from_namespaces(
            aq_commands=["message_reply", "supervisor_inbox_reply"]
        ),
        session_id=row.id,
        session_instance_token=token,
        project_id=project_id,
        elevated=True,
    )
    return row, principal


def bind(handler, db, transport):
    # The gateway's inbound root exists in Discord before AQ opens its thread.
    root_id = "900000000000000000"
    transport.messages[root_id] = SinkMessage(id=root_id, where=CHANNEL, content="operator input")
    adapter = main_mod._bind_conversation_outbox(handler.orchestrator, transport, handler.config)
    handler.orchestrator.conversation_outbox.clock = lambda: NOW
    adapter.clock = adapter.delivery.clock = lambda: NOW
    service = DigestScheduleService(
        db,
        transport,
        config=handler.config,
        lease_owner=adapter.lease_owner,
        include_outbound=True,
        conversation_adapter=adapter,
        clock=lambda: NOW,
    )
    return service


async def test_production_startup_binds_outbox_and_passes_documented_preconditions(
    monkeypatch, tmp_path, lifecycle_clock
):
    config = _postgres_config(tmp_path)
    config.discord.channel_id = CHANNEL
    config.discord.authorized_users = [AUTHOR]
    config.discord.conversation.enabled = config.messages.enabled = config.sessions.enabled = True
    adapter = _FakeAdapter([])
    adapter.bot = SimpleNamespace(_cutover_report=SimpleNamespace(status="complete"))
    events, state = _install_run_env(monkeypatch, config, adapter)
    adapter.events = events
    monkeypatch.setattr(main_mod.Orchestrator, "_get_handler", lambda self: None, raising=False)
    constructed = []
    original_init = main_mod.Orchestrator.__init__

    def capture(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.conversation_outbox = UnboundOutbox()
        self.db.count_due_escalation_deliveries = AsyncMock(return_value=0)
        constructed.append(self)

    monkeypatch.setattr(main_mod.Orchestrator, "__init__", capture)
    recovery = adapter.bot._run_conversation_backfill = AsyncMock()

    def check():
        orch = constructed[0]
        assert not isinstance(orch.conversation_outbox, UnboundOutbox)
        assert orch.digest_schedule._conversation_adapter is not None
        recovery.assert_awaited_once()
        assert conversation_preconditions(
            config, cutover_status="complete", outbox_bound=orch.conversation_outbox.bound
        ).ok
        os.kill(os.getpid(), signal.SIGTERM)

    state["on_first_cycle"] = check
    assert await lifecycle_clock(main_mod.run(str(tmp_path / "config.yaml"))) is False
    assert state["cycles"] == 1


@pytest.mark.parametrize(
    "project_live,global_live,expected",
    [
        (True, True, "supervisor-agent-queue"),
        (False, True, "supervisor-global"),
        (False, False, "conversation-queued"),
    ],
)
async def test_recipient_project_then_global_then_queued(env, project_live, global_live, expected):
    handler, db = env
    await db.create_project(Project(id="agent-queue", name="AQ"))
    await supervisor(db, "agent-queue", state="running" if project_live else "stopped")
    await supervisor(db, state="running" if global_live else "stopped")
    accepted = await post(handler)
    message = await db.get_message(accepted["supervisor_message_id"])
    assert message.to_id == expected
    assert message.project_id == (None if expected == "supervisor-global" else "agent-queue")


async def test_multiple_projects_require_explicit_discord_project_id(env):
    handler, db = env
    for project_id in ("agent-queue", "other"):
        await db.create_project(Project(id=project_id, name=project_id))
        await supervisor(db, project_id)
    await supervisor(db)
    assert await db.resolve_conversation_supervisor() == (None, None)
    handler.config.discord.project_id = "agent-queue"
    accepted = await post(handler)
    assert (
        await db.get_message(accepted["supervisor_message_id"])
    ).to_id == "supervisor-agent-queue"


def test_discord_project_id_is_loaded_from_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "database:\n  url: postgresql://u:p@localhost:5534/config_test\n"
        "discord:\n  bot_token: test-token\n  guild_id: '123'\n  project_id: agent-queue\n"
    )
    assert load_config(str(path)).discord.project_id == "agent-queue"


async def test_offline_inputs_stay_pending_then_drain_in_order(env):
    from tests.test_supervisor_inbox_commands import args

    handler, db = env
    await db.create_project(Project(id="agent-queue", name="AQ"))
    first = await post(handler)
    handler._clock = lambda: NOW + 1
    second = await post(handler, args(1))
    lens = SimpleNamespace(activity=AsyncMock(return_value="sleeping"), ensure_started=AsyncMock())
    engine = MessageDeliveryEngine(db, lens, handler.config)
    assert (await engine.run_delivery_pass())["delivered"] == 0
    lens.activity.assert_not_awaited()
    lens.ensure_started.assert_not_awaited()
    await supervisor(db, "agent-queue")
    assert await db.route_queued_conversation_inputs() == 2
    assert await db.route_queued_conversation_inputs() == 0
    pending = await db.get_pending_messages("session", "supervisor-agent-queue")
    assert [row.id for row in pending] == [
        first["supervisor_message_id"],
        second["supervisor_message_id"],
    ]


async def test_only_addressed_live_project_launch_can_reply(env):
    from dataclasses import replace

    handler, db = env
    await db.create_project(Project(id="agent-queue", name="AQ"))
    row, project = await supervisor(db, "agent-queue")
    _global_row, global_principal = await supervisor(db)
    accepted = await post(handler)
    assert (await reply(handler, reply_args(accepted), global_principal))[
        "error_code"
    ] == "out_of_scope"
    assert (
        await reply(handler, reply_args(accepted), replace(project, session_instance_token="stale"))
    )["error_code"] == "out_of_scope"
    assert (await reply(handler, reply_args(accepted), project))["success"]
    assert (
        await db.get_message(accepted["supervisor_message_id"])
    ).to_id == "supervisor-agent-queue"
    await db.update_session(row.id, state="stopped")
    assert (await reply(handler, reply_args(accepted), project))["error_code"] == "out_of_scope"


async def test_shared_dispatch_opens_thread_and_posts_one_explicit_reply(env):
    handler, db = env
    transport = SinkTransport()
    service = bind(handler, db, transport)
    accepted = await post(handler)
    await service.pump()
    conversation = await db.get_conversation(accepted["conversation_id"])
    assert conversation["state"] == "open"
    assert conversation["external_thread_id"]
    for _ in range(2):
        result = await handler.execute(
            "message_reply",
            {"message_id": accepted["supervisor_message_id"], "body": "Answer from the supervisor"},
        )
        assert result["success"]
        await service.pump()
    outbound = [message for message in transport.messages.values() if message.thread_id]
    assert len(outbound) == 2  # acknowledgement + one answer
    assert all(message.thread_id == conversation["external_thread_id"] for message in outbound)
    assert any(
        "Answer from the supervisor" in message.content for message in transport.messages.values()
    )
    duplicate = await reply(handler, reply_args(accepted, idempotency_key="another"))
    assert not duplicate["success"]


async def test_shared_dispatch_cancels_reply_after_allowlist_revocation(env):
    handler, db = env
    transport = SinkTransport()
    service = bind(handler, db, transport)
    accepted = await post(handler)
    await service.pump()
    queued_reply = await reply(handler, reply_args(accepted))
    assert queued_reply["success"]
    handler.config.discord.authorized_users = []
    await service.pump()
    rows = await db.list_outbound_deliveries(
        owner_kind="conversation", owner_id=accepted["conversation_id"]
    )
    # The frozen clock ties created_at; the hashed ID order is not action order.
    [reply_delivery] = [
        row for row in rows if row["dedup_key"] == queued_reply["delivery_dedup_key"]
    ]
    assert reply_delivery["state"] == "cancelled"
    assert len(transport.messages) == 2  # inbound root + acknowledgement


async def test_ambiguous_reply_is_not_reposted(env):
    handler, db = env
    transport = SinkTransport()
    service = bind(handler, db, transport)
    accepted = await post(handler)
    await service.pump()
    queued_reply = await reply(handler, reply_args(accepted))
    assert queued_reply["success"]
    transport.faults.append(("post_thread_message", TransportAmbiguous("lost receipt")))
    await service.pump()
    await service.pump()
    rows = await db.list_outbound_deliveries(
        owner_kind="conversation", owner_id=accepted["conversation_id"]
    )
    [reply_delivery] = [
        row for row in rows if row["dedup_key"] == queued_reply["delivery_dedup_key"]
    ]
    assert reply_delivery["state"] == "unknown"
    assert len(transport.messages) == 2


async def test_thread_permission_failure_keeps_input_and_sends_one_bounded_notice(env):
    handler, db = env
    transport = SinkTransport()
    service = bind(handler, db, transport)
    accepted = await post(handler)
    transport.faults.append(("ensure_thread", TransportUnavailable("cannot create threads")))
    await service.pump()
    await service.pump()
    assert (await db.get_conversation(accepted["conversation_id"]))["state"] == "delivery_blocked"
    assert (await db.get_conversation_input(accepted["input_id"]))["state"] == "accepted"
    notices = [
        message for message in transport.messages.values() if "Unable to open" in message.content
    ]
    assert len(notices) == 1 and notices[0].thread_id is None


async def test_exhausted_outbound_budget_preserves_unclaimed_delivery(env):
    handler, db = env
    transport = SinkTransport()
    service = bind(handler, db, transport)
    bucket = OutboundTokenBucket(clock=lambda: 0)
    for _ in range(20):
        assert bucket.take()
    handler.orchestrator._discord_bot._outbound_bucket = bucket
    service._rate_guard = main_mod._bot_rate_guard(handler.orchestrator._discord_bot)
    accepted = await post(handler)
    await service.pump()
    row = (
        await db.list_outbound_deliveries(
            owner_kind="conversation", owner_id=accepted["conversation_id"]
        )
    )[0]
    assert row["state"] == "pending" and row["attempt_count"] == 0
    assert transport.calls == []


def test_shared_outbound_bucket_counts_twenty_mutations_then_refills():
    now = [0.0]
    bucket = OutboundTokenBucket(clock=lambda: now[0])
    assert all(bucket.take() for _ in range(20))
    assert not bucket.take()
    now[0] = 3.0
    assert bucket.take()
    assert not bucket.take()


@pytest.mark.tmux
async def test_mention_project_supervisor_tmux_inbox_reply_outbox_round_trip(
    env, provider, stub_path, tmp_path
):
    handler, db = env
    await db.create_project(Project(id="agent-queue", name="AQ"))
    transport = SinkTransport()
    service = bind(handler, db, transport)
    spec = _spec(tmp_path, stub_path, name="n-supervisor--agent-queue")
    handle = await provider.start(spec)
    try:
        _row, principal = await supervisor(
            db, "agent-queue", token=handle.instance_token, work_dir=spec.work_dir
        )
        intake = SimpleNamespace(
            handle_detailed=AsyncMock(
                return_value=SimpleNamespace(consumed=False, failed=False, decision=None)
            )
        )
        router = DiscordInboundRouter(
            bot=handler.orchestrator._discord_bot,
            config=handler.config,
            handler=handler,
            escalation_intake=intake,
            diagnostics=IgnoreCounter(),
            cutover_status=lambda: "complete",
            outbox_bound=lambda: handler.orchestrator.conversation_outbox.bound,
        )
        message = SimpleNamespace(
            id=900000000000000000,
            guild=SimpleNamespace(id=int(GUILD)),
            channel=SimpleNamespace(id=int(CHANNEL)),
            author=SimpleNamespace(id=int(AUTHOR), bot=False),
            content=f"<@{BOT}> hello supervisor",
            mentions=[SimpleNamespace(id=int(BOT))],
            created_at=datetime.fromtimestamp(NOW, UTC),
            webhook_id=None,
        )
        assert await router.route(message, bot_user_id=int(BOT)) == "posted:created"
        pending = await db.get_pending_messages("session", "supervisor-agent-queue")
        assert len(pending) == 1
        lens = SessionLens(
            db=db,
            providers=SimpleNamespace(create=lambda name: provider),
            spec_builder=None,
            harness_registry=None,
            config=handler.config,
            profiles_loader=AsyncMock(),
        )
        # The raw-mode test harness is at its idle prompt; no LLM transcript exists.
        lens._transcript_activity = AsyncMock(return_value="idle")
        engine = MessageDeliveryEngine(db, lens, handler.config)
        assert (await engine.run_delivery_pass())["delivered"] == 1
        assert pending[0].id in await _received(tmp_path)
        await service.pump()
        with principal_context(principal):
            result = await handler.execute(
                "message_reply", {"message_id": pending[0].id, "body": "I received your message."}
            )
        assert result["success"]
        await service.pump()
        assert any(
            "I received your message." in item.content for item in transport.messages.values()
        )
        assert len(transport.messages) == 3  # inbound root + acknowledgement + answer
    finally:
        await provider.stop(handle, grace=0)
