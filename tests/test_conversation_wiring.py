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
from src.conversations.intake import dm_thread_id
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
    args,
    post,
    reply,
    reply_args,
)
from tests.test_tmux_integration import (
    _received,
    _spec,
)

env = inbox_tests.env
DM_CHANNEL = "999999999999999999"
lifecycle_clock = lifecycle_tests.lifecycle_clock
provider = tmux_tests.provider
stub_path = tmux_tests.stub_path


async def delivery(db, conversation_id, kind):
    """The one durable delivery row of *kind* for a conversation."""
    rows = [
        row
        for row in await db.list_outbound_deliveries(
            owner_kind="conversation", owner_id=conversation_id
        )
        if row["payload"].get("action") == kind
    ]
    assert len(rows) == 1, [(row["dedup_key"], row["state"]) for row in rows]
    return rows[0]


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


async def test_a_shared_channel_across_projects_routes_to_the_global_supervisor(env):
    """Acceptance: a message in a shared channel reaches a supervisor.

    Several projects and no ``discord.project_id`` used to resolve to
    ``(None, None)`` before the global fallback was ever consulted, so every
    turn sat on ``conversation-queued`` with nobody to drain it.  The global
    supervisor is the launch that speaks for the installation.
    """
    handler, db = env
    for project_id in ("agent-queue", "other"):
        await db.create_project(Project(id=project_id, name=project_id))
        await supervisor(db, project_id)
    global_row, _global_principal = await supervisor(db)
    assert await db.resolve_conversation_supervisor() == ("supervisor-global", None)
    accepted = await post(handler)
    message = await db.get_message(accepted["supervisor_message_id"])
    assert (message.to_id, message.project_id) == ("supervisor-global", None)
    # No live supervisor at all: the input stays queued, and says so out loud.
    await db.update_session(global_row.id, state="stopped")
    queued = await post(handler, args(1))
    assert (await db.get_message(queued["supervisor_message_id"])).to_id == "conversation-queued"
    assert [
        row["payload"]["kind"]
        for row in handler.orchestrator.conversation_outbox.rows
        if row["dedup_key"] == f"conv-notice:supervisor-missing:{queued['conversation_id']}"
    ] == ["supervisor_missing"]
    # An explicit project id still wins when the operator sets one.
    handler.config.discord.project_id = "agent-queue"
    explicit = await post(handler, args(2))
    assert (
        await db.get_message(explicit["supervisor_message_id"])
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
    answers = [
        message
        for message in transport.messages.values()
        if "Answer from the supervisor" in message.content
    ]
    assert len(answers) == 1  # the retry carried the same idempotency key
    assert answers[0].thread_id == conversation["external_thread_id"]
    # §2.4: the answer arrived before the queued-turn line was ever posted, so
    # the line is withdrawn rather than appearing under an answered message.
    assert not any(
        "Supervisor is offline" in message.content for message in transport.messages.values()
    )
    assert (await delivery(db, accepted["conversation_id"], "status_line"))["state"] == "cancelled"
    duplicate = await reply(handler, reply_args(accepted, idempotency_key="another"))
    assert not duplicate["success"]


def inbound_router(handler):
    """The gateway router, with escalation intake stubbed as not interested."""
    intake = SimpleNamespace(
        handle_detailed=AsyncMock(
            return_value=SimpleNamespace(consumed=False, failed=False, decision=None)
        )
    )
    return DiscordInboundRouter(
        bot=handler.orchestrator._discord_bot,
        config=handler.config,
        handler=handler,
        escalation_intake=intake,
        diagnostics=IgnoreCounter(),
        cutover_status=lambda: "complete",
        outbox_bound=lambda: handler.orchestrator.conversation_outbox.bound,
    )


def dm_gateway_message(index, text):
    """A direct message exactly as the gateway reports it (§2.1)."""
    return SimpleNamespace(
        id=700000000000000000 + index,
        guild=None,
        channel=SimpleNamespace(id=int(DM_CHANNEL), parent_id=None),
        author=SimpleNamespace(id=int(AUTHOR), bot=False),
        content=text,
        mentions=[],
        webhook_id=None,
        created_at=datetime.fromtimestamp(NOW, UTC),
    )


async def test_offline_status_line_is_one_post_edited_in_place_then_retired(env):
    handler, db = env
    handler.config.discord.conversation.allow_dm = True
    transport = SinkTransport()
    service = bind(handler, db, transport)
    clock = [NOW]
    adapter = service._conversation_adapter
    adapter.clock = adapter.delivery.clock = lambda: clock[0]
    service._clock = lambda: clock[0]
    router = inbound_router(handler)

    assert await router.route(
        dm_gateway_message(0, "is the release blocked?"), bot_user_id=None
    ) == ("posted:created")
    await service.pump()  # the opener posts; the line follows its own due time
    clock[0] = NOW + 40
    await service.pump()
    posted = [m for m in transport.messages.values() if "Supervisor is offline" in m.content]
    assert len(posted) == 1 and "1 message queued" in posted[0].content
    conversation = await db.find_conversation_by_thread(
        transport="discord", channel_id=DM_CHANNEL, external_thread_id=dm_thread_id(DM_CHANNEL)
    )

    inputs = await db.list_conversation_inputs(conversation["id"])

    # A second queued message edits that one post rather than adding a line.
    handler._clock = lambda: NOW + 41
    assert await router.route(dm_gateway_message(1, "and the replica?"), bot_user_id=None) == (
        "posted:created"
    )
    clock[0] = NOW + 42
    await service.pump()
    assert [m.id for m in transport.messages.values() if "Supervisor is offline" in m.content] == [
        posted[0].id
    ]
    assert "2 messages queued" in transport.messages[posted[0].id].content
    assert transport.edits and transport.edits[-1][0] == posted[0].id

    # The answer supersedes it: the line is retired, not left claiming the
    # supervisor is away.
    handler._clock = lambda: NOW + 43
    answerable = {
        "conversation_id": conversation["id"],
        "input_id": inputs[0]["id"],
    }
    assert (await reply(handler, reply_args(answerable)))["success"]
    clock[0] = NOW + 44
    await service.pump()
    assert posted[0].id not in transport.messages
    assert not any("Supervisor is offline" in m.content for m in transport.messages.values())


def channel_gateway_message(index, text, *, mentions=()):
    """A top-level message in the configured channel, exactly as reported."""
    return SimpleNamespace(
        id=800000000000000000 + index,
        guild=SimpleNamespace(id=int(GUILD)),
        channel=SimpleNamespace(id=int(CHANNEL), parent_id=None),
        author=SimpleNamespace(id=int(AUTHOR), bot=False),
        content=text,
        mentions=[SimpleNamespace(id=int(user)) for user in mentions],
        webhook_id=None,
        created_at=datetime.fromtimestamp(NOW, UTC),
    )


async def test_a_plain_message_in_the_channel_is_answered_there_and_opens_no_thread(env):
    """Acceptance: the main chat works, without threads.

    One plain message in the channel, answered in the channel.  The
    acknowledgement and the answer are ordinary posts that reply to the human's
    message for context, and nothing calls the thread creator.
    """
    handler, db = env
    handler.config.discord.conversation.require_mention = False
    await db.create_project(Project(id="agent-queue", name="AQ"))
    await supervisor(db, "agent-queue")
    transport = SinkTransport()
    service = bind(handler, db, transport)
    router = inbound_router(handler)
    root = "800000000000000000"

    assert await router.route(
        channel_gateway_message(0, "why is the release blocked?"), bot_user_id=None
    ) == "posted:created"
    await service.pump()
    conversation = await db.find_channel_conversation(transport="discord", channel_id=CHANNEL)
    assert (conversation["kind"], conversation["external_thread_id"]) == ("channel", None)
    # The acknowledgement is a channel post replying to the human's message.
    acks = [m for m in transport.messages.values() if "will answer here" in m.content]
    assert len(acks) == 1
    assert (acks[0].where, acks[0].thread_id, acks[0].reference_message_id) == (
        CHANNEL,
        None,
        root,
    )

    # The supervisor's answer comes back to the same channel, not a thread.
    inputs = await db.list_conversation_inputs(conversation["id"])
    handler._clock = lambda: NOW + 30
    assert (
        await reply(
            handler, reply_args({"conversation_id": conversation["id"], "input_id": inputs[0]["id"]})
        )
    )["success"]
    await service.pump()
    answers = [m for m in transport.messages.values() if "Answer from the supervisor" in m.content]
    assert len(answers) == 1
    assert (answers[0].where, answers[0].thread_id, answers[0].reference_message_id) == (
        CHANNEL,
        None,
        root,
    )
    assert transport.threads == {}, "no thread is ever created for a channel conversation"
    assert "ensure_thread" not in transport.calls
    assert (await db.get_conversation(conversation["id"]))["state"] == "open"


async def test_a_channel_message_with_no_supervisor_running_says_so_in_the_channel(env):
    """Nothing may sit on conversation-queued without saying so in the channel."""
    handler, db = env
    handler.config.discord.conversation.require_mention = False
    transport = SinkTransport()
    service = bind(handler, db, transport)
    router = inbound_router(handler)

    assert await router.route(
        channel_gateway_message(0, "why is the release blocked?"), bot_user_id=None
    ) == "posted:created"
    await service.pump()
    conversation = await db.find_channel_conversation(transport="discord", channel_id=CHANNEL)
    assert (
        await db.get_message(
            (await db.list_conversation_inputs(conversation["id"]))[0]["supervisor_message_id"]
        )
    ).to_id == "conversation-queued"
    posted = [m for m in transport.messages.values() if "No supervisor is running" in m.content]
    assert len(posted) == 1
    assert (posted[0].where, posted[0].thread_id) == (CHANNEL, None)
    assert transport.threads == {}

    # A supervisor appearing drains the queue by itself; no reply required.
    await db.create_project(Project(id="agent-queue", name="AQ"))
    await supervisor(db, "agent-queue")
    assert await db.route_queued_conversation_inputs() == 1


async def test_a_follow_up_in_the_channel_continues_the_same_conversation(env):
    handler, db = env
    handler.config.discord.conversation.require_mention = False
    await db.create_project(Project(id="agent-queue", name="AQ"))
    await supervisor(db, "agent-queue")
    transport = SinkTransport()
    service = bind(handler, db, transport)
    router = inbound_router(handler)

    assert await router.route(channel_gateway_message(0, "and the replica?"), bot_user_id=None) == (
        "posted:created"
    )
    conversation = await db.find_channel_conversation(transport="discord", channel_id=CHANNEL)
    handler._clock = lambda: NOW + 10
    assert await router.route(
        channel_gateway_message(1, "still blocked?"), bot_user_id=None
    ) == "posted:created"

    # One conversation, two turns, and the second answers to the first message.
    assert (await db.get_conversation(conversation["id"]))["updated_at"] == NOW + 10
    assert len(await db.list_conversation_inputs(conversation["id"])) == 2
    await service.pump()
    assert transport.threads == {}
    # Every post this conversation makes replies to its first message.
    assert {
        message.reference_message_id
        for message in transport.messages.values()
        if message.id != "900000000000000000"
    } == {"800000000000000000"}


async def test_a_direct_message_conversation_answers_in_its_own_channel(env):
    from src.conversations.intake import DM_GUILD

    handler, db = env
    handler.config.discord.conversation.allow_dm = True
    transport = SinkTransport()
    service = bind(handler, db, transport)
    router = inbound_router(handler)

    assert await router.route(dm_gateway_message(0, "hello"), bot_user_id=None) == "posted:created"
    conversation = await db.get_conversation(
        (
            await db.find_conversation_by_thread(
                transport="discord",
                channel_id=DM_CHANNEL,
                external_thread_id=dm_thread_id(DM_CHANNEL),
            )
        )["id"]
    )
    assert (conversation["guild_id"], conversation["kind"]) == (DM_GUILD, "channel")
    await service.pump()
    # No thread is opened in a direct message: the ack posts in the DM itself.
    # The second post is the §2.4 notice that no supervisor is running.
    assert transport.calls == ["post_root", "post_root"]
    assert {message.where for message in transport.messages.values()} == {CHANNEL, DM_CHANNEL}
    assert (await db.get_conversation(conversation["id"]))["state"] == "open"


async def test_a_direct_message_is_refused_once_the_opt_in_is_withdrawn(env):
    from src.conversations.intake import DM_GUILD, dm_thread_id

    handler, _ = env
    with principal_context(ExecutionPrincipal.service("discord-gateway")):
        result = await handler.execute(
            "supervisor_inbox_post",
            {
                "envelope": {
                    "transport": "discord",
                    "guild_id": DM_GUILD,
                    "channel_id": DM_CHANNEL,
                    "external_message_id": "700000000000000000",
                    "external_root_message_id": "700000000000000000",
                    "external_thread_id": dm_thread_id(DM_CHANNEL),
                    "author_id": AUTHOR,
                    "text": "hello",
                    "received_at": NOW,
                    "mentions_bot": False,
                },
                "source": "gateway",
            },
        )
    assert result["error_code"] == "dm"


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
    assert len(transport.messages) == 3  # inbound root + acknowledgement + no-supervisor notice


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
    assert len(transport.messages) == 3


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


@pytest.mark.tmux
async def test_channel_message_without_a_mention_reaches_the_supervisor_tmux(
    env, provider, stub_path, tmp_path
):
    """§2.2 in a live session: no @agent-queue, one turn, one answer, one thread."""
    handler, db = env
    await db.create_project(Project(id="agent-queue", name="AQ"))
    handler.config.discord.conversation.require_mention = False
    transport = SinkTransport()
    service = bind(handler, db, transport)
    # The operator's plain message already exists in the channel.
    transport.messages["900000000000000001"] = SinkMessage(
        id="900000000000000001", where=CHANNEL, content="what is blocking the release?"
    )
    spec = _spec(tmp_path, stub_path, name="n-supervisor--agent-queue")
    handle = await provider.start(spec)
    try:
        _row, principal = await supervisor(
            db, "agent-queue", token=handle.instance_token, work_dir=spec.work_dir
        )
        router = DiscordInboundRouter(
            bot=handler.orchestrator._discord_bot,
            config=handler.config,
            handler=handler,
            escalation_intake=SimpleNamespace(
                handle_detailed=AsyncMock(
                    return_value=SimpleNamespace(consumed=False, failed=False, decision=None)
                )
            ),
            diagnostics=IgnoreCounter(),
            cutover_status=lambda: "complete",
            outbox_bound=lambda: handler.orchestrator.conversation_outbox.bound,
        )
        plain = SimpleNamespace(
            id=900000000000000001,
            guild=SimpleNamespace(id=int(GUILD)),
            channel=SimpleNamespace(id=int(CHANNEL), parent_id=None),
            author=SimpleNamespace(id=int(AUTHOR), bot=False),
            content="what is blocking the release?",
            mentions=[],
            created_at=datetime.fromtimestamp(NOW, UTC),
            webhook_id=None,
        )
        assert await router.route(plain, bot_user_id=int(BOT)) == "posted:created"

        conversation = await db.get_conversation(
            (await db.find_channel_conversation(transport="discord", channel_id=CHANNEL))["id"]
        )
        assert conversation["kind"] == "channel"
        pending = await db.get_pending_messages("session", "supervisor-agent-queue")
        assert [row.id for row in pending] == [
            (await db.list_conversation_inputs(conversation["id"]))[0]["supervisor_message_id"]
        ]
        lens = SessionLens(
            db=db,
            providers=SimpleNamespace(create=lambda name: provider),
            spec_builder=None,
            harness_registry=None,
            config=handler.config,
            profiles_loader=AsyncMock(),
        )
        lens._transcript_activity = AsyncMock(return_value="idle")
        engine = MessageDeliveryEngine(db, lens, handler.config)
        assert (await engine.run_delivery_pass())["delivered"] == 1
        assert pending[0].id in await _received(tmp_path)

        await service.pump()
        assert conversation["external_thread_id"] is None
        bound = await db.get_conversation(conversation["id"])
        assert bound["external_thread_id"] and bound["state"] == "open"
        with principal_context(principal):
            result = await handler.execute(
                "message_reply", {"message_id": pending[0].id, "body": "Nothing needs you."}
            )
        assert result["success"]
        await service.pump()
        answers = [
            item for item in transport.messages.values() if "Nothing needs you." in item.content
        ]
        assert len(answers) == 1
        assert answers[0].thread_id == bound["external_thread_id"]
    finally:
        await provider.stop(handle, grace=0)
