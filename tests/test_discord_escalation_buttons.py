"""§5.3's choice buttons: what a post offers, and what a press records.

Two halves, tested apart and then together:

* :func:`choice_buttons` and :func:`parse_custom_id` are pure, so "at most five
  choices, then ``Reply…``", "a label over 80 characters is cut" and "a press
  names an option rather than carrying one" are table assertions;
* :class:`~src.discord.escalation_buttons.EscalationButtonPress` runs against
  the real command boundary and a real database, so the acceptance question --
  an allow-listed press records a verified reply and a stranger's press records
  nothing -- is answered by rows rather than by a mock.

No test here touches a gateway: the transport is exercised against fakes that
record the component set a post was sent with, which is the only part of
Discord the rest of this feature depends on.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig, DiscordConfig, DiscordEscalationConfig
from src.database import Database
from src.database.queries.escalation_queries import TERMINAL_ESCALATION_STATES
from src.discord.bot import AgentQueueBot
from src.discord.escalation_buttons import (
    NOT_OUR_BUTTON,
    OUTCOME_RECORDED,
    OUTCOME_REFUSED,
    OUTCOME_REPLAYED,
    REPLY_GUIDE,
    EscalationButtonPress,
    build_view,
)
from src.discord.escalation_transport import DiscordEscalationTransport
from src.escalations.interactions import (
    KIND_CHOICE,
    KIND_REPLY,
    MAX_BUTTON_LABEL_CHARS,
    MAX_CHOICE_BUTTONS,
    REPLY_BUTTON_LABEL,
    choice_buttons,
    choice_text,
    custom_id_for,
    parse_custom_id,
)
from src.models import AgentProfile, Project, SessionRecord
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

JACK = "111111111111111111"
STRANGER = "999999999999999999"
CHANNEL = "424242424242424242"
ROOT_MESSAGE = "500500500500500500"


# --- the pure half: what the post offers -------------------------------------


def test_five_choices_then_the_reply_affordance():
    specs = choice_buttons("escalation-1", [f"option {n}" for n in range(7)])

    assert [spec.kind for spec in specs] == [KIND_CHOICE] * MAX_CHOICE_BUTTONS + [KIND_REPLY]
    assert [spec.choice_index for spec in specs] == [0, 1, 2, 3, 4, None]
    assert specs[-1].label == REPLY_BUTTON_LABEL


def test_an_incident_with_no_choices_offers_no_buttons():
    assert choice_buttons("escalation-1", []) == ()
    assert choice_buttons("escalation-1", ["", "   "]) == ()


def test_a_label_over_discords_ceiling_is_cut_not_refused():
    long = "x" * 200
    specs = choice_buttons("escalation-1", [long, "two\nlines\tand   spaces"])
    assert len(specs[0].label) == MAX_BUTTON_LABEL_CHARS
    assert specs[0].label.endswith("…")
    assert specs[1].label == "two lines and spaces"


def test_every_rendered_id_parses_back_to_the_option_it_names():
    specs = choice_buttons("escalation-abc", ["keep the trial", "revert"])
    for spec in specs:
        press = parse_custom_id(spec.custom_id)
        assert press is not None
        assert press.escalation_id == "escalation-abc"
        assert press.kind == spec.kind
        assert press.choice_index == spec.choice_index


@pytest.mark.parametrize(
    "custom_id",
    [
        "aqesc:escalation-1:choice",  # a choice with no index
        "aqesc:escalation-1:reply:0",  # Reply… with an index
        "aqesc:escalation-1:choice:9:9",
        "other:escalation-1:choice:0",  # not this feature's namespace
        "",
        "aqesc:escalation:1:choice:0",  # the id itself may not carry the separator
        "aqesc::choice:0",
    ],
)
def test_a_custom_id_that_does_not_name_exactly_one_button_is_refused(custom_id):
    assert parse_custom_id(custom_id) is None


def test_an_unrenderable_incident_id_yields_no_buttons():
    assert choice_buttons("esc with spaces", ["keep"]) == ()
    assert custom_id_for("esc with spaces", KIND_CHOICE, 0) == "not an escalation button"


def test_the_recorded_text_is_the_stored_choice_not_anything_the_client_sent():
    press = parse_custom_id("aqesc:escalation-1:choice:1")
    assert choice_text(("keep the trial", "revert to Sonnet"), press) == "revert to Sonnet"
    assert choice_text(("only option",), press) is None
    assert choice_text((), press) is None
    assert choice_text(("a", "b"), parse_custom_id("aqesc:escalation-1:reply")) is None


# --- the transport half: what Discord is asked to render ---------------------


class FakeTracker:
    def should_allow(self, *, critical: bool = True) -> bool:
        return True

    def record(self, status: int) -> None:  # pragma: no cover - fault paths only
        pass


def make_transport():
    bot = SimpleNamespace(_rate_tracker=FakeTracker(), get_channel=lambda _id: None, user=None)
    config = AppConfig()
    config.discord = DiscordConfig(
        channel_id=CHANNEL, escalation=DiscordEscalationConfig(enabled=True)
    )
    return DiscordEscalationTransport(bot, config)


class RecordingMessage:
    def __init__(self, message_id: int = 7) -> None:
        self.id = message_id
        self.edits: list[dict] = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class RecordingChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.message = RecordingMessage()

    async def send(self, content, **kwargs):
        self.sent.append({"content": content, **kwargs})
        return self.message

    async def fetch_message(self, _id: int):
        return self.message


async def test_a_root_post_carries_its_buttons_and_releases_the_view():
    transport = make_transport()
    channel = RecordingChannel()
    transport._bot.get_channel = lambda _id: channel
    specs = choice_buttons("escalation-1", ["keep the trial", "revert"])

    outcome = await transport.post_root(
        channel_id=CHANNEL, content="needs a decision", buttons=specs
    )

    view = channel.sent[0]["view"]
    assert outcome.root_message_id == "7"
    assert view is not None and view.timeout is None
    assert [item.custom_id for item in view.children] == [spec.custom_id for spec in specs]
    assert [item.label for item in view.children] == [spec.label for spec in specs]
    # The components are on the message; the in-memory dispatch entry is not, so
    # a press is handled by the bot rather than by a view that dies with us.
    assert view.is_finished()


async def test_an_edit_replaces_the_button_set_and_can_remove_it():
    transport = make_transport()
    channel = RecordingChannel()
    transport._bot.get_channel = lambda _id: channel
    specs = choice_buttons("escalation-1", ["keep the trial"])

    await transport.edit_root(
        channel_id=CHANNEL, root_message_id="7", content="answered", buttons=specs
    )
    await transport.edit_root(
        channel_id=CHANNEL, root_message_id="7", content="✅ Resolved: kept the trial"
    )

    kept, removed = channel.message.edits
    assert [item.custom_id for item in kept["view"].children] == [
        spec.custom_id for spec in specs
    ]
    assert removed["view"].children == []
    assert removed["view"].timeout is None


async def test_build_view_is_none_when_a_post_offers_nothing():
    assert build_view([]) is None


# --- the press half: who may answer, and what is recorded --------------------


@pytest.fixture
async def env(tmp_path):
    db = Database(lease_dsn("escalation-buttons"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project P"))
    await db.create_profile(
        AgentProfile(
            id="supervisor",
            name="Supervisor",
            harness="codex",
            lifecycle="named",
            aq_commands=["escalation_create", "escalation_reply"],
            harness_tools=[],
            plugin_tools=[],
            needs_workspace=False,
        )
    )
    await db.create_session(
        SessionRecord(
            id="super-p",
            project_id="p",
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="n-supervisor--p",
            lifecycle="named",
            work_dir=str(tmp_path),
            epoch="epoch",
            instance_token="super-token",
            started_at=time.time(),
            state="running",
        )
    )
    config = AppConfig(
        discord=DiscordConfig(
            bot_token="t",
            guild_id="1",
            authorized_users=[JACK],
            escalation=DiscordEscalationConfig(enabled=True),
        ),
        database=DatabaseConfig(url=lease_dsn("escalation-buttons")),
        data_dir=str(tmp_path / "data"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.git = MagicMock()
    yield CommandHandler(orch, config), db, config
    await db.close()


def interaction(*, custom_id: str, user_id: str = JACK, message_id: str = ROOT_MESSAGE, pk="900"):
    """The gateway's view of one button press, with nothing the presser asserts."""
    return SimpleNamespace(
        id=pk,
        type=discord.InteractionType.component,
        data={"custom_id": custom_id},
        user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(id=message_id) if message_id is not None else None,
        response=SimpleNamespace(
            is_done=lambda: False, send_message=AsyncMock(return_value=None)
        ),
        followup=SimpleNamespace(send=AsyncMock(return_value=None)),
    )


async def incident_with_choices(db, handler, *, choices=("keep the trial", "revert to Sonnet")):
    created = await handler.execute(
        "escalation_create",
        {
            "project_id": "p",
            "source_kind": "core",
            "source_identity": "incident-1",
            "incident_key": "incident-1",
            "summary": "Two release branches claim the fix",
            "investigation": "Both were built from main",
            "decision_requested": "Which branch ships?",
            "choices": list(choices),
            "severity": "high",
        },
    )
    incident = created["escalation"]
    await db.seed_legacy_escalation_root(
        incident["id"], channel_id=CHANNEL, root_message_id=ROOT_MESSAGE, available_at=1.0
    )
    return incident


async def inbound_rows(db, incident):
    return [
        row
        for row in await db.list_escalation_messages(incident["id"])
        if row["direction"] == "inbound"
    ]


async def test_an_allow_listed_press_records_the_answer_the_supervisor_will_apply(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    reconciled: list[str] = []
    press = EscalationButtonPress(
        handler, config, reconcile=AsyncMock(side_effect=reconciled.append)
    )
    click = interaction(custom_id=f"aqesc:{incident['id']}:choice:1")

    outcome = await press.handle(click)

    assert outcome == OUTCOME_RECORDED
    recorded = await inbound_rows(db, incident)
    assert [row["text"] for row in recorded] == ["revert to Sonnet"]
    assert recorded[0]["verified_actor"] == f"human:discord:{JACK}"
    assert recorded[0]["external_message_id"] == f"interaction:{click.id}"
    # §5.3: the post moves to answered, which is the reply_received state.
    assert (await db.get_escalation(incident["id"]))["state"] == "reply_received"
    assert reconciled == [incident["id"]]
    ack = click.response.send_message.await_args
    assert ack.kwargs["ephemeral"] is True
    assert "revert to Sonnet" in ack.args[0]


async def test_a_stranger_press_records_nothing_and_only_they_are_told(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    ignored: list[str] = []
    press = EscalationButtonPress(handler, config, on_ignore=ignored.append)
    click = interaction(custom_id=f"aqesc:{incident['id']}:choice:0", user_id=STRANGER)

    outcome = await press.handle(click)

    assert outcome == OUTCOME_REFUSED
    assert ignored == ["not_authorized"]
    assert await inbound_rows(db, incident) == []
    assert (await db.get_escalation(incident["id"]))["state"] == "needs_human"
    message = click.response.send_message.await_args.args[0]
    assert "not on this channel" in message
    assert click.response.send_message.await_args.kwargs["ephemeral"] is True


async def test_a_replayed_press_records_once(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    press = EscalationButtonPress(handler, config)
    custom_id = f"aqesc:{incident['id']}:choice:0"

    first = await press.handle(interaction(custom_id=custom_id, pk="901"))
    replay = await press.handle(interaction(custom_id=custom_id, pk="901"))

    assert first == OUTCOME_RECORDED
    assert replay == OUTCOME_REPLAYED
    assert len(await inbound_rows(db, incident)) == 1


async def test_a_press_on_another_posts_post_is_refused(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    press = EscalationButtonPress(handler, config)

    outcome = await press.handle(
        interaction(custom_id=f"aqesc:{incident['id']}:choice:0", message_id="123123")
    )

    assert outcome == OUTCOME_REFUSED
    assert await inbound_rows(db, incident) == []


async def test_a_press_naming_an_option_the_row_no_longer_has_is_refused(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler, choices=("keep the trial",))
    press = EscalationButtonPress(handler, config)

    outcome = await press.handle(interaction(custom_id=f"aqesc:{incident['id']}:choice:1"))

    assert outcome == OUTCOME_REFUSED
    assert await inbound_rows(db, incident) == []


async def test_a_press_on_a_closed_incident_is_history_not_a_new_answer(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    press = EscalationButtonPress(handler, config)
    click = interaction(custom_id=f"aqesc:{incident['id']}:choice:0")
    await press.handle(click)
    reply = await db.get_escalation(incident["id"])
    await db.transition_escalation(
        incident["id"],
        expected_revision=reply["revision"],
        new_state="resolving",
    )
    resolving = await db.get_escalation(incident["id"])
    await db.transition_escalation(
        incident["id"],
        expected_revision=resolving["revision"],
        new_state="resolved",
        terminal_outcome="kept the trial",
        outcome="human",
    )

    outcome = await press.handle(click)

    assert outcome == OUTCOME_REFUSED
    assert len(await inbound_rows(db, incident)) == 1
    assert "already closed" in click.response.send_message.await_args.args[0]


async def test_the_reply_affordance_points_at_the_thread_and_records_nothing(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    press = EscalationButtonPress(handler, config)
    click = interaction(custom_id=f"aqesc:{incident['id']}:reply")

    outcome = await press.handle(click)

    assert outcome == REPLY_GUIDE
    assert await inbound_rows(db, incident) == []
    assert "thread" in click.response.send_message.await_args.args[0]


async def test_a_button_of_another_feature_is_handed_back_untouched(env):
    handler, _db, config = env
    press = EscalationButtonPress(handler, config)
    click = interaction(custom_id="aqmemory:approve")

    outcome = await press.handle(click)

    assert outcome == NOT_OUR_BUTTON
    click.response.send_message.assert_not_awaited()


async def test_an_unknown_incident_is_refused_before_anything_is_written(env):
    handler, db, config = env
    press = EscalationButtonPress(handler, config)

    outcome = await press.handle(interaction(custom_id="aqesc:escalation-nope:choice:0"))

    assert outcome == OUTCOME_REFUSED
    assert await db.list_escalation_messages("escalation-nope") == []


# --- the wiring: the bot is the one entry point for a press ------------------


def make_bot(handler, config):
    bot = AgentQueueBot.__new__(AgentQueueBot)
    bot.config = config
    bot.orchestrator = SimpleNamespace(
        _command_handler=handler, escalation_delivery=None
    )
    bot._intake_diagnostics = SimpleNamespace(record=lambda code: None)
    bot._escalation_buttons_impl = None
    bot._cutover_complete = SimpleNamespace(is_set=lambda: True)
    return bot


async def test_the_bot_routes_our_button_press_to_the_command_boundary(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    bot = make_bot(handler, config)

    click = interaction(custom_id=f"aqesc:{incident['id']}:choice:0")

    await AgentQueueBot.on_interaction(bot, click)

    assert [row["text"] for row in await inbound_rows(db, incident)] == ["keep the trial"]


async def test_the_bot_ignores_a_foreign_interaction_and_an_ungated_press(env):
    handler, db, config = env
    incident = await incident_with_choices(db, handler)
    bot = make_bot(handler, config)

    await AgentQueueBot.on_interaction(bot, interaction(custom_id="aqmemory:approve"))
    before = await db.list_escalation_messages(incident["id"])
    bot._cutover_complete = SimpleNamespace(is_set=lambda: False)
    click = interaction(custom_id=f"aqesc:{incident['id']}:choice:0")
    await AgentQueueBot.on_interaction(bot, click)

    assert await db.list_escalation_messages(incident["id"]) == before


def test_terminal_states_stay_immutable_for_the_press_handler():
    """The press path reads the same terminal vocabulary the state machine uses."""
    assert {"resolved", "cancelled", "stale"} == set(TERMINAL_ESCALATION_STATES)