"""Inbound escalation-thread correlation (discord-simplification §5, §7).

Nothing here touches a Discord gateway: the transport half is driven through
:class:`~src.escalations.transport.SinkTransport` so the thread binding these
tests correlate against is a real confirmed delivery receipt, and the Discord
message is a stand-in with exactly the fields a gateway reports.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig, DiscordConfig, DiscordEscalationConfig
from src.database import Database
from src.discord.escalation_intake import DiscordEscalationIntake
from src.escalations import (
    EscalationDeliveryService,
    EscalationFacts,
    SinkTransport,
    render_ack,
)
from src.escalations.intake import (
    ACTION_ACCEPT,
    ACTION_CLOSED,
    ACTION_IGNORE,
    IGNORE_LOG_FORMAT,
    MAX_REPLY_CHARS,
    REASON_CODES,
    InboundMessage,
    classify_inbound,
)
from src.escalations.plan import ack_dedup_key
from src.models import Project
from tests.db_fixtures import lease_dsn

BASE_URL = "https://queue.example.test"
CHANNEL = "424242424242424242"
THREAD = "515151515151515151"
HUMAN = "111111111111111111"
STRANGER = "999999999999999999"
BOT_USER_ID = 777777777777777777


def make_discord_config(*, channel_id: str = CHANNEL, enabled: bool = True) -> DiscordConfig:
    return DiscordConfig(
        channel_id=channel_id,
        authorized_users=[HUMAN],
        escalation=DiscordEscalationConfig(enabled=enabled),
    )


def make_app_config(**kwargs) -> AppConfig:
    config = AppConfig()
    config.discord = make_discord_config(**kwargs)
    return config


def observed(**overrides) -> InboundMessage:
    values = {
        "transport": "discord",
        "external_message_id": "m-1",
        "channel_id": CHANNEL,
        "thread_id": THREAD,
        "author_id": HUMAN,
        "text": "Roll the replica forward.",
    }
    values.update(overrides)
    return InboundMessage(**values)


def binding_row(*, state: str = "needs_human", **overrides) -> dict:
    row = {
        "id": "esc-1",
        "project_id": "p",
        "state": state,
        "delivery_channel_id": CHANNEL,
        "delivery_thread_id": THREAD,
        "delivery_generation": 0,
    }
    row.update(overrides)
    return row


def classify(message, *, binding=None, channel=CHANNEL, allowed=(HUMAN,), enabled=True):
    return classify_inbound(
        message,
        configured_channel_id=channel,
        authorized_author_ids=allowed,
        binding=binding,
        enabled=enabled,
    )


# ------------------------------------------------------- pure classification


def test_an_authorized_human_reply_in_a_bound_thread_is_accepted():
    decision = classify(observed(), binding=binding_row())
    assert decision.action == ACTION_ACCEPT
    assert decision.escalation_id == "esc-1"
    assert decision.project_id == "p"
    assert decision.correlated


@pytest.mark.parametrize(
    ("message", "reason"),
    [
        (observed(thread_id=None), "message is not in a thread"),
        (observed(channel_id="303030303030303030"), "thread is not in the configured channel"),
        (observed(author_id=STRANGER), "author is not on the escalation reply allowlist"),
        (observed(author_id=""), "author is not on the escalation reply allowlist"),
        (observed(author_is_bot=True), "message was authored by a bot"),
        (observed(is_own_message=True), "message was authored by this bot"),
    ],
)
def test_chatter_mentions_dms_spoofed_identities_and_bot_loops_never_create_work(message, reason):
    decision = classify(message, binding=binding_row())
    assert decision.action == ACTION_IGNORE
    assert decision.reason == reason
    assert decision.escalation_id is None


def test_an_unknown_thread_is_refused_even_for_an_authorized_human():
    decision = classify(observed(), binding=None)
    assert decision.action == ACTION_IGNORE
    assert decision.reason == "thread is not bound to an escalation"


def test_a_binding_that_disagrees_with_the_gateway_is_refused():
    wrong_thread = classify(observed(), binding=binding_row(delivery_thread_id="6" * 18))
    wrong_channel = classify(observed(), binding=binding_row(delivery_channel_id="6" * 18))
    assert wrong_thread.reason == "bound thread disagrees with the observed thread"
    assert wrong_channel.reason == "bound channel disagrees with the observed channel"


def test_reconfiguring_the_channel_stops_correlating_the_old_thread():
    decision = classify(observed(), binding=binding_row(), channel="3" * 18)
    assert decision.action == ACTION_IGNORE
    assert decision.reason == "thread is not in the configured channel"


def test_an_unconfigured_or_disabled_surface_correlates_nothing():
    assert classify(observed(), binding=binding_row(), channel=None).reason == (
        "no configured channel"
    )
    assert classify(observed(), binding=binding_row(), enabled=False).reason == (
        "escalation intake is disabled"
    )


def test_empty_oversized_and_unidentified_messages_are_refused():
    assert classify(observed(text="   "), binding=binding_row()).reason == "reply has no text"
    assert "exceeds" in classify(observed(text="x" * 16001), binding=binding_row()).reason
    assert (
        classify(observed(external_message_id=""), binding=binding_row()).reason
        == "message has no transport identity"
    )


@pytest.mark.parametrize("state", ["resolved", "cancelled", "stale"])
def test_a_late_reply_to_a_closed_incident_is_correlated_but_not_work(state):
    decision = classify(observed(), binding=binding_row(state=state))
    assert decision.action == ACTION_CLOSED
    assert decision.escalation_id == "esc-1"
    assert decision.reason == f"escalation is {state}"


def test_closed_state_guidance_does_not_claim_a_supervisor_is_reviewing():
    facts = EscalationFacts(
        id="esc-1",
        project_id="p",
        state="resolved",
        revision=3,
        severity="high",
        summary="s",
        investigation="i",
        decision_requested="d",
    )
    text = render_ack(facts, dedup_key=ack_dedup_key("esc-1", "m-1"))
    assert "already resolved" in text
    assert "has not reopened the work" in text
    assert "reviewing it" not in text

    open_text = render_ack(
        EscalationFacts(
            id="esc-1",
            project_id="p",
            state="needs_human",
            revision=0,
            severity="high",
            summary="s",
            investigation="i",
            decision_requested="d",
        ),
        dedup_key=ack_dedup_key("esc-1", "m-1"),
    )
    assert "supervisor is reviewing it" in open_text


# ---------------------------------------------------------- durable wiring


@pytest.fixture
async def env(tmp_path):
    from unittest.mock import MagicMock

    from src.commands.handler import CommandHandler
    from src.config import DatabaseConfig
    from src.event_bus import EventBus
    from src.orchestrator import Orchestrator

    dsn = lease_dsn("escalation-intake")
    db = Database(dsn)
    await db.initialize()
    await db.create_project(Project(id="p", name="Project P"))
    config = AppConfig(
        discord=make_discord_config(),
        workspace_dir=str(tmp_path / "workspaces"),
        database=DatabaseConfig(url=dsn),
        data_dir=str(tmp_path / "data"),
    )
    config.discord.bot_token = "test"
    config.discord.guild_id = "1"
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    orch.bus = EventBus(env="dev")
    yield CommandHandler(orch, config), db, config
    await db.close()


async def make_incident(db: Database, **overrides):
    values = {
        "id": "esc-1",
        "project_id": "p",
        "task_id": "t-1",
        "source_kind": "task_attempt",
        "source_identity": "attempt-1",
        "incident_key": "task:t-1:attempt:attempt-1",
        "supervisor_owner": "supervisor-p",
        "task_title": "Deploy the migration",
        "task_status": "BLOCKED",
        "summary": "The migration cannot apply on the replica",
        "investigation": "Reproduced on staging",
        "decision_requested": "Roll forward or hold?",
        "severity": "high",
        "now": 10.0,
    }
    values.update(overrides)
    row, _ = await db.create_escalation(**values)
    return row


async def post_incident(db: Database, incident, *, config=None):
    """Drive the real outbox until the incident owns a channel post and thread.

    Returns the sink and the thread ID the transport actually assigned, which
    is the durable binding every correlation test then replies into.
    """
    sink = SinkTransport()
    service = EscalationDeliveryService(
        db,
        sink,
        config=config or make_discord_config(),
        lease_owner="daemon-a",
        base_url=BASE_URL,
    )
    await service.reconcile(incident)
    await service.pump()
    root = next(
        row
        for row in await db.list_escalation_deliveries(incident["id"])
        if row["kind"] == "root"
    )
    return sink, service, str(root["thread_id"])


class RecordingHandler:
    """Wraps a real CommandHandler so a test can see command name and principal."""

    def __init__(self, handler, db, *, execute=True):
        self._handler = handler
        self.db = db
        self._execute = execute
        self.calls: list[tuple[str, dict, object]] = []

    async def execute(self, command: str, args: dict):
        from src.commands.principal import current_principal

        self.calls.append((command, dict(args), current_principal()))
        if not self._execute:
            return {"success": True, "created": True}
        return await self._handler.execute(command, args)


def discord_message(
    *,
    author_id=HUMAN,
    parent_id=CHANNEL,
    thread_id=THREAD,
    text="Roll forward.",
    message_id="m-1",
    is_bot=False,
):
    channel = SimpleNamespace(id=thread_id, parent_id=parent_id)
    author = SimpleNamespace(id=author_id, bot=is_bot)
    return SimpleNamespace(id=message_id, channel=channel, author=author, content=text)


async def test_a_thread_reply_reaches_the_core_with_gateway_derived_identity(env):
    handler, db, config = env
    incident = await make_incident(db)
    _, _, thread_id = await post_incident(db, incident)

    recorder = RecordingHandler(handler, db, execute=False)
    intake = DiscordEscalationIntake(recorder, config)

    assert await intake.handle(discord_message(thread_id=thread_id), bot_user_id=BOT_USER_ID)
    command, args, principal = recorder.calls[0]
    assert command == "escalation_reply"
    assert args == {
        "escalation_id": "esc-1",
        "text": "Roll forward.",
        "external_message_id": "m-1",
    }
    # §5: identity comes from trusted server context, never from a body field.
    assert not {"actor", "actor_id", "human", "verified_actor", "project_id"} & set(args)
    assert principal.kind is PrincipalKind.SERVICE
    assert principal.service_name == f"discord:{HUMAN}"


async def test_the_reply_becomes_a_verified_human_message_and_a_supervisor_notice(env):
    handler, db, config = env
    incident = await make_incident(db)
    _, _, thread_id = await post_incident(db, incident)
    intake = DiscordEscalationIntake(handler, config)

    assert await intake.handle(discord_message(thread_id=thread_id), bot_user_id=BOT_USER_ID)

    inbound = [
        m for m in await db.list_escalation_messages("esc-1") if m["direction"] == "inbound"
    ]
    assert len(inbound) == 1
    assert inbound[0]["verified_actor"] == f"human:discord:{HUMAN}"
    assert inbound[0]["external_message_id"] == "m-1"
    assert inbound[0]["supervisor_message_id"] is not None
    queued = await db.get_message(inbound[0]["supervisor_message_id"])
    assert queued.to_id == "supervisor-p"
    assert queued.to_kind == "session"
    assert queued.body == "Roll forward."
    # The notice addresses the logical owner and is undelivered: waking or
    # recreating that supervisor is the existing delivery machinery's job.
    assert queued.delivered_at is None
    assert (await db.get_escalation("esc-1"))["state"] == "reply_received"


async def test_a_redelivered_message_and_a_second_reply_stay_ordered_and_idempotent(env):
    handler, db, config = env
    incident = await make_incident(db)
    _, _, thread_id = await post_incident(db, incident)
    intake = DiscordEscalationIntake(handler, config)

    first = discord_message(thread_id=thread_id, message_id="m-1", text="Roll forward.")
    await intake.handle(first, bot_user_id=BOT_USER_ID)
    await intake.handle(first, bot_user_id=BOT_USER_ID)  # gateway reconnect replay
    await intake.handle(
        discord_message(thread_id=thread_id, message_id="m-2", text="Actually hold."),
        bot_user_id=BOT_USER_ID,
    )

    inbound = [
        m for m in await db.list_escalation_messages("esc-1") if m["direction"] == "inbound"
    ]
    assert [m["external_message_id"] for m in inbound] == ["m-1", "m-2"]
    assert [m["text"] for m in inbound] == ["Roll forward.", "Actually hold."]
    # Each accepted reply queued the owning supervisor exactly once.
    assert len({m["supervisor_message_id"] for m in inbound}) == 2


async def test_each_accepted_reply_is_acknowledged_in_the_thread_exactly_once(env):
    handler, db, config = env
    incident = await make_incident(db)
    sink, service, thread_id = await post_incident(db, incident)
    intake = DiscordEscalationIntake(handler, config, reconcile=service.reconcile)

    message = discord_message(thread_id=thread_id)
    await intake.handle(message, bot_user_id=BOT_USER_ID)
    await intake.handle(message, bot_user_id=BOT_USER_ID)
    await service.pump()
    await service.pump()

    acks = [m for m in sink.messages.values() if "reviewing it" in m.content]
    assert len(acks) == 1
    assert acks[0].thread_id == thread_id


async def test_a_late_reply_to_a_closed_incident_gets_guidance_and_reopens_nothing(env):
    handler, db, config = env
    incident = await make_incident(db)
    sink, service, thread_id = await post_incident(db, incident)
    await db.transition_escalation(
        "esc-1",
        expected_revision=int(incident["revision"]),
        new_state="cancelled",
        terminal_outcome="Fixed out of band",
    )
    # Deliver the resolution first, so the reply arrives into a thread §7 has
    # already posted the outcome in and archived — the real "late reply" shape.
    await service.reconcile("esc-1")
    await service.pump()
    await service.pump()
    assert thread_id in sink.archived

    intake = DiscordEscalationIntake(handler, config, reconcile=service.reconcile)
    assert await intake.handle(discord_message(thread_id=thread_id), bot_user_id=BOT_USER_ID)
    await service.pump()
    await service.pump()

    assert (await db.get_escalation("esc-1"))["state"] == "cancelled"
    inbound = [
        m for m in await db.list_escalation_messages("esc-1") if m["direction"] == "inbound"
    ]
    assert len(inbound) == 1
    assert inbound[0]["supervisor_message_id"] is None  # no supervisor work was queued
    guidance = [m for m in sink.messages.values() if "has not reopened the work" in m.content]
    assert len(guidance) == 1
    assert guidance[0].thread_id == thread_id
    # Guidance un-archives the thread to post; §7's archived state is restored.
    assert thread_id in sink.archived


async def test_correlation_survives_a_restart_because_it_reads_stored_ids(env):
    handler, db, config = env
    incident = await make_incident(db)
    _, _, thread_id = await post_incident(db, incident)

    # A brand-new adapter with brand-new in-memory state — the daemon restarted
    # and kept no thread map of its own.
    recorder = RecordingHandler(handler, db, execute=False)
    intake = DiscordEscalationIntake(recorder, config)
    assert await intake.handle(discord_message(thread_id=thread_id), bot_user_id=BOT_USER_ID)
    assert recorder.calls[0][1]["escalation_id"] == "esc-1"


async def test_a_thread_in_a_channel_that_is_no_longer_configured_is_ignored(env):
    handler, db, config = env
    incident = await make_incident(db)
    _, _, thread_id = await post_incident(db, incident)

    config.discord.channel_id = "303030303030303030"
    recorder = RecordingHandler(handler, db, execute=False)
    intake = DiscordEscalationIntake(recorder, config)
    assert not await intake.handle(discord_message(thread_id=thread_id), bot_user_id=BOT_USER_ID)
    assert recorder.calls == []
    assert await db.list_escalation_messages("esc-1") == []


async def test_channel_chatter_never_costs_a_database_lookup(env):
    handler, db, config = env

    def forbidden(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("uncorrelatable chatter reached the database")

    recorder = RecordingHandler(handler, db, execute=False)
    recorder.db = SimpleNamespace(find_escalation_by_thread=forbidden)
    intake = DiscordEscalationIntake(recorder, config)

    # No thread, wrong channel, unauthorized author, and the bot's own post.
    assert not await intake.handle(
        discord_message(parent_id=None, thread_id=CHANNEL), bot_user_id=BOT_USER_ID
    )
    assert not await intake.handle(
        discord_message(parent_id="303030303030303030"), bot_user_id=BOT_USER_ID
    )
    assert not await intake.handle(discord_message(author_id=STRANGER), bot_user_id=BOT_USER_ID)
    assert not await intake.handle(
        discord_message(author_id=BOT_USER_ID, is_bot=True), bot_user_id=BOT_USER_ID
    )
    assert recorder.calls == []


async def test_a_reply_in_a_thread_that_is_not_ours_is_left_to_the_ordinary_routing(env):
    handler, db, config = env
    await make_incident(db)  # exists, but was never posted: no thread binding
    recorder = RecordingHandler(handler, db, execute=False)
    intake = DiscordEscalationIntake(recorder, config)

    assert not await intake.handle(
        discord_message(thread_id="606060606060606060"), bot_user_id=BOT_USER_ID
    )
    assert recorder.calls == []


async def test_the_core_refuses_a_reply_whose_identity_is_not_server_derived(env):
    """A caller with no verified principal cannot smuggle one in through args."""
    handler, db, _config = env
    incident = await make_incident(db)
    await post_incident(db, incident)

    with principal_context(ExecutionPrincipal.service("cascade")):
        result = await handler.execute(
            "escalation_reply",
            {"escalation_id": "esc-1", "text": "hi", "external_message_id": "m-9"},
        )
    assert result["error_code"] == "human_evidence_required"

    with principal_context(ExecutionPrincipal.service(f"discord:{HUMAN}")):
        spoofed = await handler.execute(
            "escalation_reply",
            {
                "escalation_id": "esc-1",
                "text": "hi",
                "external_message_id": "m-9",
                "verified_actor": f"human:discord:{STRANGER}",
            },
        )
    assert spoofed["error_code"] == "spoofed_identity"
    assert await db.list_escalation_messages("esc-1") == []


async def test_a_stale_thread_binding_cannot_correlate_after_the_incident_is_gone(env):
    """The binding is a join, so a deleted incident stops correlating outright."""
    _handler, db, _config = env
    incident = await make_incident(db)
    _, _, thread_id = await post_incident(db, incident)
    assert await db.find_escalation_by_thread(channel_id=CHANNEL, thread_id=thread_id)
    assert (
        await db.find_escalation_by_thread(channel_id=CHANNEL, thread_id="606060606060606060")
    ) is None
    assert (
        await db.find_escalation_by_thread(channel_id="303030303030303030", thread_id=thread_id)
    ) is None
    assert await db.find_escalation_by_thread(channel_id="", thread_id=thread_id) is None


def test_the_inbound_path_never_calls_a_task_or_worker_command():
    """§5/§7: a Discord reply may not mutate a task or type into a session."""
    from pathlib import Path

    source = Path("src/discord/escalation_intake.py").read_text(encoding="utf-8")
    for forbidden in (
        "task_close",
        "task_reopen",
        "task_set",
        "task_comment",
        "inject_message",
        "session_send",
        "gate_resolve",
        "playbook_resume",
        "escalation_apply_reply",
        "escalation_update",
    ):
        assert forbidden not in source, f"{forbidden} must not be reachable from Discord intake"
    assert source.count('"escalation_reply"') == 1


# ------------------------------------------------ reason codes and ignore log

IGNORE_CODES_IN_GATE_ORDER = [
    "disabled",
    "own_message",
    "bot_author",
    "not_in_thread",
    "no_channel",
    "foreign_channel",
    "author_not_allowlisted",
    "thread_unbound",
    "binding_channel_mismatch",
    "binding_thread_mismatch",
    "empty_text",
    "oversize",
    "no_message_id",
]


def test_the_reason_code_table_is_stable_and_in_gate_order():
    # Codes are what operators grep for and what the digest counts by: a
    # rename is a contract change, not a refactor.
    assert list(REASON_CODES.values()) == IGNORE_CODES_IN_GATE_ORDER


@pytest.mark.parametrize(
    ("message", "overrides", "code"),
    [
        (observed(), {"enabled": False}, "disabled"),
        (observed(is_own_message=True), {}, "own_message"),
        (observed(author_is_bot=True), {}, "bot_author"),
        (observed(thread_id=None), {}, "not_in_thread"),
        (observed(), {"channel": None}, "no_channel"),
        (observed(channel_id="303030303030303030"), {}, "foreign_channel"),
        (observed(author_id=STRANGER), {}, "author_not_allowlisted"),
        (observed(), {"binding": None}, "thread_unbound"),
        (
            observed(),
            {"binding": binding_row(delivery_channel_id="6" * 18)},
            "binding_channel_mismatch",
        ),
        (
            observed(),
            {"binding": binding_row(delivery_thread_id="6" * 18)},
            "binding_thread_mismatch",
        ),
        (observed(text="   "), {}, "empty_text"),
        (observed(text="x" * (MAX_REPLY_CHARS + 1)), {}, "oversize"),
        (observed(external_message_id=""), {}, "no_message_id"),
    ],
)
def test_every_ignore_carries_a_stable_code(message, overrides, code):
    kwargs = {"binding": binding_row(), **overrides}
    decision = classify(message, **kwargs)
    assert decision.action == ACTION_IGNORE
    assert decision.code == code
    assert REASON_CODES[decision.reason] == code


def test_accepted_and_closed_decisions_carry_codes():
    assert classify(observed(), binding=binding_row()).code == "accepted"
    assert classify(observed(), binding=binding_row(state="resolved")).code == "closed"


def test_every_ignore_reason_string_has_a_code():
    # The table is the contract: a new _ignore(...) without a code is a bug.
    import ast
    import inspect

    from src.escalations import intake

    tree = ast.parse(inspect.getsource(intake))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_ignore" and node.args
    ]
    reasons = {call.args[0].value for call in calls if isinstance(call.args[0], ast.Constant)}
    assert reasons <= set(REASON_CODES)
    assert len(calls) == len(REASON_CODES)


def test_observe_records_the_guild_the_gateway_reported():
    intake = DiscordEscalationIntake(SimpleNamespace(), make_app_config())
    message = discord_message()
    message.guild = SimpleNamespace(id=1)
    assert intake.observe(message, bot_user_id=BOT_USER_ID).guild_id == "1"
    direct = discord_message()
    assert intake.observe(direct, bot_user_id=BOT_USER_ID).guild_id is None


def ignore_line(code, *, guild="1", channel=CHANNEL, message="m-1", author=HUMAN) -> str:
    return (
        f"discord intake ignored reason={code} guild={guild} channel={channel} "
        f"message={message} author={author}"
    )


def _ignore_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "src.discord.escalation_intake"
        and record.levelno == logging.INFO
        and record.getMessage().startswith("discord intake ignored ")
    ]


async def test_an_ignored_message_logs_ids_and_code_but_never_content(caplog):
    def forbidden(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("uncorrelatable chatter reached the database")

    handler = SimpleNamespace(db=SimpleNamespace(find_escalation_by_thread=forbidden))
    intake = DiscordEscalationIntake(handler, make_app_config())
    secret = "the launch codes are 1234"
    message = discord_message(parent_id=None, thread_id=CHANNEL, text=secret)
    message.guild = SimpleNamespace(id=1)
    with caplog.at_level(logging.INFO, logger="src.discord.escalation_intake"):
        assert not await intake.handle(message, bot_user_id=BOT_USER_ID)
    assert _ignore_lines(caplog) == [ignore_line("not_in_thread")]
    assert secret not in caplog.text


async def test_an_unbound_thread_logs_after_the_lookup(caplog):
    async def unbound(**kwargs):
        return None

    handler = SimpleNamespace(db=SimpleNamespace(find_escalation_by_thread=unbound))
    intake = DiscordEscalationIntake(handler, make_app_config())
    message = discord_message(text="Roll forward, secretly.")
    message.guild = SimpleNamespace(id=1)
    with caplog.at_level(logging.INFO, logger="src.discord.escalation_intake"):
        assert not await intake.handle(message, bot_user_id=BOT_USER_ID)
    assert _ignore_lines(caplog) == [ignore_line("thread_unbound")]
    assert "secretly" not in caplog.text


async def test_a_consumed_reply_logs_no_ignore_line(caplog):
    async def bound(**kwargs):
        return binding_row()

    handler = SimpleNamespace(
        db=SimpleNamespace(find_escalation_by_thread=bound),
        calls=[],
    )

    async def execute(command, args):
        handler.calls.append(command)
        return {"success": True, "created": False}

    handler.execute = execute
    intake = DiscordEscalationIntake(handler, make_app_config())
    with caplog.at_level(logging.INFO, logger="src.discord.escalation_intake"):
        assert await intake.handle(discord_message(), bot_user_id=BOT_USER_ID)
    assert handler.calls == ["escalation_reply"]
    assert _ignore_lines(caplog) == []


async def test_a_classify_failure_logs_classify_error(caplog):
    intake = DiscordEscalationIntake(SimpleNamespace(), make_app_config())
    message = discord_message(text="do not log me")
    message.guild = SimpleNamespace(id=1)

    async def broken(observed):
        raise RuntimeError("db down")

    intake.classify = broken
    with caplog.at_level(logging.INFO, logger="src.discord.escalation_intake"):
        assert not await intake.handle(message, bot_user_id=BOT_USER_ID)
    assert _ignore_lines(caplog) == [ignore_line("classify_error")]
    assert "do not log me" not in caplog.text


async def test_a_message_the_adapter_cannot_observe_still_logs_its_ids(caplog):
    intake = DiscordEscalationIntake(SimpleNamespace(), make_app_config())
    # No channel at all: observe() itself fails, before any decision exists.
    message = SimpleNamespace(
        id="m-7",
        guild=SimpleNamespace(id=1),
        author=SimpleNamespace(id=HUMAN, bot=False),
        content="unobservable",
    )
    with caplog.at_level(logging.INFO, logger="src.discord.escalation_intake"):
        assert not await intake.handle(message, bot_user_id=BOT_USER_ID)
    assert _ignore_lines(caplog) == [ignore_line("classify_error", channel=None, message="m-7")]
    assert "unobservable" not in caplog.text


def test_the_ignore_log_format_names_ids_and_the_code_only():
    assert IGNORE_LOG_FORMAT == (
        "discord intake ignored reason=%s guild=%s channel=%s message=%s author=%s"
    )
