"""One-way, idempotent migration of legacy Discord conversations."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.discord.cutover import LegacyBinding, LegacyConversationMigrator, run_discord_cutover
from src.models import Project, Task
from tests.db_fixtures import lease_dsn


class EmptyDB:
    async def list_agent_questions(self, **_kwargs):
        return []

    async def list_gates(self, **_kwargs):
        return []

    async def list_tasks(self, **_kwargs):
        return []


class FakeChannel:
    def __init__(self, channel_id, name, message=None):
        self.id = channel_id
        self.name = name
        self.message = message

    async def fetch_message(self, _message_id):
        if self.message is None:
            raise LookupError("missing")
        return self.message

    async def history(self, **_kwargs):
        if False:
            yield None


def fake_bot(discord, channels):
    by_id = {channel.id: channel for channel in channels}
    return SimpleNamespace(
        config=AppConfig(discord=discord),
        orchestrator=SimpleNamespace(db=EmptyDB()),
        _guild=SimpleNamespace(text_channels=channels),
        user=SimpleNamespace(id=99),
        get_channel=lambda channel_id: by_id.get(channel_id),
        fetch_channel=lambda channel_id: by_id.get(channel_id),
    )


@pytest.fixture
async def db():
    database = Database(lease_dsn("discord-cutover"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Project"))
    await database.create_task(
        Task(id="t", project_id="p", title="Needs a decision", description="Wait for a human")
    )
    yield database
    await database.close()


async def add_question(db, question_id, *, state, answer=None, requires_human=True):
    await db.create_agent_question(
        id=question_id,
        session_id="worker-session",
        session_name="worker",
        instance_token="worker-token",
        task_id="t",
        project_id="p",
        agent_id="worker-agent",
        claim_epoch=4,
        turn_id=f"turn-{question_id}",
        question="Which reviewed option should I use?",
        reason="The worker cannot infer human intent.",
        requires_human=requires_human,
        state=state,
        answer=answer,
        answered_by="discord:42" if answer else None,
        created_at=1.0,
        updated_at=2.0,
        source_ts=1.0,
    )


async def test_pending_conversations_migrate_once_and_old_state_is_unchanged(db):
    await add_question(db, "q-open", state="human")
    # Old accepted rows may no longer retain the pre-answer requires_human
    # marker; the accepted state and evidence still have to survive.
    await add_question(
        db,
        "q-answered",
        state="answered",
        answer="Use option B",
        requires_human=False,
    )
    gate_id, _ = await db.create_gate(
        "p", "human", "Approve rollout", question="Proceed?", await_id="legacy-gate"
    )
    await db.create_gate("p", "timer", "Wait", await_id="machine-gate")
    binding = LegacyBinding("question", "q-open", "123", "456")
    migrator = LegacyConversationMigrator(db, clock=lambda: 10.0)

    first = await migrator.run(channel_id="123", bindings=[binding], inert_messages=1)
    second = await migrator.run(channel_id="123", bindings=[binding], inert_messages=1)

    assert (first.migrated_questions, first.migrated_gates) == (2, 1)
    assert first.accepted_answers_preserved == 1
    assert first.adopted_roots == 1
    assert (second.migrated_questions, second.migrated_gates) == (0, 0)
    assert second.accepted_answers_preserved == 0
    assert second.adopted_roots == 0

    incidents = await db.list_escalations(project_id="p")
    assert {(row["source_kind"], row["source_identity"]) for row in incidents} == {
        ("question", "q-open"),
        ("question", "q-answered"),
        ("gate", gate_id),
    }
    accepted = await db.get_escalation("escalation-q-answered")
    assert accepted["state"] == "resolved"
    assert accepted["terminal_evidence"]["redelivery"] is False
    history = await db.list_escalation_messages("escalation-q-answered")
    assert [(row["text"], row["transport"]) for row in history] == [
        ("Use option B", "discord-legacy")
    ]
    assert await db.list_escalation_deliveries("escalation-q-answered") == []
    assert await db.get_pending_messages("session", "supervisor-p") == []

    open_question = await db.get_agent_question("q-open")
    answered_question = await db.get_agent_question("q-answered")
    assert open_question["state"] == "human"
    assert answered_question["state"] == "answered"
    assert (await db.get_gate(gate_id))["status"] == "open"


async def test_a_legacy_root_is_adopted_only_in_the_selected_channel(db):
    await add_question(db, "q-other-channel", state="human")
    report = await LegacyConversationMigrator(db, clock=lambda: 10.0).run(
        channel_id="123",
        bindings=[LegacyBinding("question", "q-other-channel", "999", "456")],
    )

    assert report.migrated_questions == 1
    assert report.adopted_roots == 0
    assert await db.list_escalation_deliveries("escalation-q-other-channel") == []


async def test_one_legacy_destination_name_maps_to_its_unique_channel_id():
    config = DiscordConfig(bot_token="t", guild_id="1")
    config._legacy_destination_names = ("shared",)
    config._legacy_inventory_names = ("shared",)
    bot = fake_bot(config, [FakeChannel(123, "shared")])

    report = await run_discord_cutover(bot)

    assert report.status == "complete"
    assert report.channel_id == "123"
    assert config.channel_id == "123"


async def test_conflicting_legacy_destinations_never_choose_a_channel():
    config = DiscordConfig(bot_token="t", guild_id="1")
    config._legacy_destination_names = ("control", "notifications")
    bot = fake_bot(
        config,
        [FakeChannel(123, "control"), FakeChannel(456, "notifications")],
    )

    report = await run_discord_cutover(bot)

    assert report.status == "needs_configuration"
    assert config.channel_id == ""
    assert "legacy destinations disagree" in report.conflicts[0]


async def test_bot_owned_legacy_question_view_is_cleared_before_routing_goes_live(db):
    await add_question(db, "q-card", state="human")
    await db.mark_agent_question_notified("q-card", "123", "456")
    message = SimpleNamespace(
        id=456,
        author=SimpleNamespace(id=99),
        embeds=[],
        edit=AsyncMock(),
    )
    channel = FakeChannel(123, "shared", message)
    config = DiscordConfig(bot_token="t", guild_id="1", channel_id="123")
    bot = fake_bot(config, [channel])
    bot.orchestrator = SimpleNamespace(db=db)

    report = await run_discord_cutover(bot)

    message.edit.assert_awaited_once_with(view=None)
    assert report.inert_messages == 1
    assert report.adopted_roots == 1
    assert (await db.get_agent_question("q-card"))["state"] == "human"
