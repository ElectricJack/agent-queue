"""The Discord half of escalation delivery, without a gateway.

Only the parts that are genuinely Discord-shaped are tested here: how an HTTP
failure is classified into the port's fault taxonomy, and what the client is
allowed to mention.  Everything about *when* to send lives in
``tests/test_escalation_delivery.py`` and runs against the sink transport.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import discord
from src.config import AppConfig, DiscordConfig, DiscordEscalationConfig
from src.discord.escalation_transport import DiscordEscalationTransport, _allowed_mentions
from src.escalations.transport import (
    TransportMissing,
    TransportRetryable,
    TransportUnavailable,
)

USER_ID = "111111111111111111"
ROLE_ID = "222222222222222222"


class FakeTracker:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.recorded: list[int] = []

    def should_allow(self, *, critical: bool = True) -> bool:
        return self.allow

    def record(self, status: int) -> None:
        self.recorded.append(status)


def make_transport(*, allow: bool = True) -> tuple[DiscordEscalationTransport, FakeTracker]:
    tracker = FakeTracker(allow)
    bot = SimpleNamespace(_rate_tracker=tracker, get_channel=lambda _id: None, user=None)
    config = AppConfig()
    config.discord = DiscordConfig(
        channel_id="424242424242424242",
        escalation=DiscordEscalationConfig(mention_user_ids=[USER_ID], mention_role_ids=[ROLE_ID]),
    )
    return DiscordEscalationTransport(bot, config), tracker


def http(status: int) -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=status, reason=""), "boom")


def test_only_the_configured_ids_may_ever_be_mentioned():
    allowed = _allowed_mentions(
        DiscordEscalationConfig(mention_user_ids=[USER_ID], mention_role_ids=[ROLE_ID])
    )
    assert allowed.everyone is False
    assert allowed.replied_user is False
    assert [obj.id for obj in allowed.users] == [int(USER_ID)]
    assert [obj.id for obj in allowed.roles] == [int(ROLE_ID)]


def test_no_configured_mention_means_no_mention_at_all():
    allowed = _allowed_mentions(DiscordEscalationConfig())
    assert allowed.everyone is False
    assert allowed.users == [] and allowed.roles == []


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (403, TransportUnavailable),
        (404, TransportMissing),
        (429, TransportRetryable),
        (500, TransportRetryable),
        (400, TransportRetryable),
    ],
)
def test_http_failures_are_classified_for_the_dispatcher(status, expected):
    transport, tracker = make_transport()
    assert isinstance(transport._http_error(http(status)), expected)
    if status in (401, 403, 429):
        assert status in tracker.recorded


async def test_a_hot_rate_guard_holds_the_send_instead_of_dropping_it():
    transport, _ = make_transport(allow=False)
    with pytest.raises(TransportRetryable, match="rate guard"):
        await transport.post_root(channel_id="424242424242424242", content="x")


async def test_a_non_numeric_channel_is_an_actionable_configuration_fault():
    transport, _ = make_transport()
    with pytest.raises(TransportUnavailable, match="not an ID"):
        await transport.post_root(channel_id="agent-queue", content="x")


class FakeMessage:
    """A root post that may or may not already own a thread."""

    def __init__(self, thread=None) -> None:
        self.thread = thread
        self.created: list[str] = []

    async def create_thread(self, *, name: str):
        self.created.append(name)
        return SimpleNamespace(id=999, send=None)


class FakeChannel:
    def __init__(self, message: FakeMessage) -> None:
        self.message = message
        self.sent: list[str] = []

    async def fetch_message(self, _id: int) -> FakeMessage:
        return self.message

    async def send(self, content, **_kwargs):  # pragma: no cover - guard only
        self.sent.append(content)
        raise AssertionError("ensure_thread must not send anything")


def bind(transport, channel: FakeChannel) -> None:
    transport._bot.get_channel = lambda _id: channel


async def test_ensure_thread_creates_the_thread_and_sends_nothing():
    transport, _ = make_transport()
    channel = FakeChannel(FakeMessage())
    bind(transport, channel)

    handle = await transport.ensure_thread(
        channel_id="424242424242424242", root_message_id="7", name="incident"
    )

    assert (handle.thread_id, handle.created) == ("999", True)
    assert channel.message.created == ["incident"]
    assert channel.sent == []


async def test_ensure_thread_rebinds_an_existing_thread_instead_of_making_a_second():
    transport, _ = make_transport()
    channel = FakeChannel(FakeMessage(thread=SimpleNamespace(id=555)))
    bind(transport, channel)

    handle = await transport.ensure_thread(
        channel_id="424242424242424242", root_message_id="7", name="incident"
    )

    assert (handle.thread_id, handle.created) == ("555", False)
    assert channel.message.created == []
    assert channel.sent == []
