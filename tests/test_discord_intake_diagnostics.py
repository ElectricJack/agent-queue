"""Hour-window counts of ignored inbound Discord messages, by reason code.

The one INFO line per ignored message answers "why did *this* message vanish";
the counter answers "what is the gateway ignoring, and how much of it", which
``digest_status`` reports as its ``intake`` block.  It is in-memory on purpose:
a restart forgets it, and it must never grow with the channel's traffic.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.config import AppConfig, DiscordConfig
from src.discord.bot import AgentQueueBot
from src.discord.escalation_intake import CLASSIFY_ERROR_CODE, DiscordEscalationIntake
from src.discord.intake_diagnostics import (
    EMPTY_SNAPSHOT,
    MAX_EVENTS,
    WINDOW_SECONDS,
    IgnoreCounter,
)

CHANNEL = "424242424242424242"
THREAD = "515151515151515151"
HUMAN = "111111111111111111"
BOT_USER_ID = 777777777777777777


# ------------------------------------------------------------------ counter


def test_counts_by_code_within_the_window_only():
    now = [1000.0]
    counter = IgnoreCounter(clock=lambda: now[0])
    counter.record("not_in_thread")
    counter.record("not_in_thread")
    counter.record("bot_author")
    now[0] += 3599.0
    assert counter.snapshot() == {
        "available": True,
        "window_seconds": 3600,
        "total": 3,
        "ignored": {"bot_author": 1, "not_in_thread": 2},
    }
    now[0] += 2.0
    assert counter.snapshot() == {
        "available": True,
        "window_seconds": 3600,
        "total": 0,
        "ignored": {},
    }


def test_each_event_leaves_the_window_on_its_own_schedule():
    now = [0.0]
    counter = IgnoreCounter(window_seconds=60, clock=lambda: now[0])
    counter.record("not_in_thread")
    now[0] = 30.0
    counter.record("bot_author")
    now[0] = 61.0
    assert counter.snapshot()["ignored"] == {"bot_author": 1}
    now[0] = 91.0
    assert counter.snapshot()["total"] == 0


def test_the_counter_is_bounded():
    counter = IgnoreCounter(max_events=3, clock=lambda: 0.0)
    for index in range(10):
        counter.record("old" if index < 7 else "new")
    # The oldest events are the ones forgotten.
    assert counter.snapshot()["ignored"] == {"new": 3}
    assert counter.snapshot()["total"] == 3


def test_the_defaults_are_an_hour_and_ten_thousand_events():
    assert WINDOW_SECONDS == 3600.0
    assert MAX_EVENTS == 10_000
    counter = IgnoreCounter(clock=lambda: 0.0)
    for _ in range(MAX_EVENTS + 5):
        counter.record("not_in_thread")
    assert counter.snapshot()["total"] == MAX_EVENTS


def test_a_fresh_counter_is_available_and_empty():
    assert IgnoreCounter(clock=lambda: 0.0).snapshot() == {
        "available": True,
        "window_seconds": 3600,
        "total": 0,
        "ignored": {},
    }


def test_empty_snapshot_shape_matches_live_shape():
    assert EMPTY_SNAPSHOT == {"available": False, "window_seconds": 3600, "total": 0, "ignored": {}}
    assert set(EMPTY_SNAPSHOT) == set(IgnoreCounter(clock=lambda: 0.0).snapshot())


def test_a_snapshot_is_a_copy_the_caller_cannot_corrupt():
    counter = IgnoreCounter(clock=lambda: 0.0)
    counter.record("bot_author")
    counter.snapshot()["ignored"]["bot_author"] = 99
    assert counter.snapshot()["ignored"] == {"bot_author": 1}


# ------------------------------------------------------- intake feeds it


def make_app_config() -> AppConfig:
    return AppConfig(discord=DiscordConfig(channel_id=CHANNEL, authorized_users=[HUMAN]))


def discord_message(*, parent_id=CHANNEL, thread_id=THREAD, text="Roll forward."):
    return SimpleNamespace(
        id="m-1",
        content=text,
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=thread_id, parent_id=parent_id),
        author=SimpleNamespace(id=HUMAN, bot=False),
    )


async def test_every_ignored_message_is_counted_by_its_code():
    counter = IgnoreCounter(clock=lambda: 0.0)

    async def unbound(**kwargs):
        return None

    handler = SimpleNamespace(db=SimpleNamespace(find_escalation_by_thread=unbound))
    intake = DiscordEscalationIntake(handler, make_app_config(), on_ignore=counter.record)

    assert not await intake.handle(
        discord_message(parent_id=None, thread_id=CHANNEL), bot_user_id=BOT_USER_ID
    )
    assert not await intake.handle(discord_message(), bot_user_id=BOT_USER_ID)

    async def broken(observed):
        raise RuntimeError("db down")

    intake.classify = broken
    assert not await intake.handle(discord_message(), bot_user_id=BOT_USER_ID)

    assert counter.snapshot()["ignored"] == {
        CLASSIFY_ERROR_CODE: 1,
        "not_in_thread": 1,
        "thread_unbound": 1,
    }


async def test_a_consumed_reply_is_not_counted():
    counter = IgnoreCounter(clock=lambda: 0.0)

    async def bound(**kwargs):
        return {
            "id": "esc-1",
            "project_id": "p",
            "state": "needs_human",
            "delivery_channel_id": CHANNEL,
            "delivery_thread_id": THREAD,
            "delivery_generation": 0,
        }

    async def execute(command, args):
        return {"success": True, "created": False}

    handler = SimpleNamespace(db=SimpleNamespace(find_escalation_by_thread=bound), execute=execute)
    intake = DiscordEscalationIntake(handler, make_app_config(), on_ignore=counter.record)
    assert await intake.handle(discord_message(), bot_user_id=BOT_USER_ID)
    assert counter.snapshot()["total"] == 0


async def test_a_failing_ignore_hook_never_changes_the_decision():
    def exploding(code):
        raise RuntimeError("counter broke")

    intake = DiscordEscalationIntake(SimpleNamespace(), make_app_config(), on_ignore=exploding)
    assert not await intake.handle(
        discord_message(parent_id=None, thread_id=CHANNEL), bot_user_id=BOT_USER_ID
    )

    async def broken(observed):
        raise RuntimeError("db down")

    intake.classify = broken
    assert not await intake.handle(discord_message(), bot_user_id=BOT_USER_ID)


async def test_the_bot_owns_one_counter_its_intake_feeds(monkeypatch):
    from src.discord import rate_guard

    # Constructing the bot must not reconfigure the process-wide rate tracker.
    monkeypatch.setattr(rate_guard, "configure_tracker", lambda **kwargs: None)
    handler = SimpleNamespace(db=SimpleNamespace())
    bot = AgentQueueBot(make_app_config(), SimpleNamespace(_command_handler=handler))
    assert isinstance(bot._intake_diagnostics, IgnoreCounter)

    intake = bot._escalation_intake()
    assert not await intake.handle(
        discord_message(parent_id=None, thread_id=CHANNEL), bot_user_id=BOT_USER_ID
    )
    assert bot._intake_diagnostics.snapshot()["ignored"] == {"not_in_thread": 1}

    # A handler swap rebuilds the adapter; the counts survive it.
    bot.orchestrator._command_handler = SimpleNamespace(db=SimpleNamespace())
    rebuilt = bot._escalation_intake()
    assert rebuilt is not intake
    assert not await rebuilt.handle(
        discord_message(parent_id=None, thread_id=CHANNEL), bot_user_id=BOT_USER_ID
    )
    assert bot._intake_diagnostics.snapshot()["ignored"] == {"not_in_thread": 2}
