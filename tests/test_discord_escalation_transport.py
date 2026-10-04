"""The Discord half of escalation delivery, without a gateway.

Only the parts that are genuinely Discord-shaped are tested here: how an HTTP
failure is classified into the port's fault taxonomy, what the client is
allowed to mention, and how content is fitted under the 2000-character
message ceiling.  Everything about *when* to send lives in
``tests/test_escalation_delivery.py`` and runs against the sink transport.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

import discord
from src.config import AppConfig, DiscordConfig, DiscordEscalationConfig
from src.discord.escalation_transport import DiscordEscalationTransport, _allowed_mentions
from src.escalations.transport import (
    MAX_CONTENT_CHARS,
    TRUNCATION_NOTICE,
    TransportMissing,
    TransportRejected,
    TransportRetryable,
    TransportUnavailable,
    bound_content,
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


def http(status: int, message: str | dict = "boom") -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=status, reason=""), message)


#: What Discord answered for the rev-bright-ridge notice (2026-10-03).
TOO_LONG = {
    "code": 50035,
    "message": "Invalid Form Body",
    "errors": {
        "content": {
            "_errors": [
                {"code": "BASE_TYPE_MAX_LENGTH", "message": "Must be 2000 or fewer in length."}
            ]
        }
    },
}


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
        (502, TransportRetryable),
        (401, TransportRetryable),
        (400, TransportRejected),
        (413, TransportRejected),
    ],
)
def test_http_failures_are_classified_for_the_dispatcher(status, expected):
    transport, tracker = make_transport()
    assert isinstance(transport._http_error(http(status)), expected)
    if status in (401, 403, 429):
        assert status in tracker.recorded


def test_a_validation_400_is_rejected_for_good_and_names_discords_error_code():
    """Resending the same invalid form body gets the same 400 forever."""
    transport, _ = make_transport()
    error = transport._http_error(http(400, TOO_LONG))
    assert isinstance(error, TransportRejected)
    assert not isinstance(error, TransportRetryable)
    assert "50035" in str(error) and "2000 or fewer" in str(error)


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


# ------------------------------------------------------------ content bound


class RecordingMessage:
    def __init__(self, content: str = "") -> None:
        self.id = 77
        self.content = content
        self.edits: list[str] = []

    async def edit(self, *, content: str, **_kwargs) -> None:
        self.edits.append(content)


class RecordingChannel:
    """A channel (or thread) that keeps what it was asked to send."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.message = RecordingMessage()

    async def send(self, content, **_kwargs):
        if len(content) > MAX_CONTENT_CHARS:
            raise http(400, TOO_LONG)
        self.sent.append(content)
        return RecordingMessage(content)

    async def fetch_message(self, _id: int) -> RecordingMessage:
        return self.message


def oversized(last_line: str) -> str:
    return "\n".join(["**Heading**", "x" * 5000, last_line])


async def test_post_root_fits_an_oversized_post_and_keeps_its_marker_line():
    transport, _ = make_transport()
    channel = RecordingChannel()
    bind(transport, channel)

    await transport.post_root(
        channel_id="424242424242424242", content=oversized("-# escalation `e` · aq-esc:k")
    )

    [content] = channel.sent
    assert len(content) <= MAX_CONTENT_CHARS
    assert content.startswith("**Heading**\n")
    assert content.endswith("\n-# escalation `e` · aq-esc:k")
    assert TRUNCATION_NOTICE in content


async def test_post_thread_message_fits_an_oversized_message_and_keeps_its_marker_line():
    transport, _ = make_transport()
    thread = RecordingChannel()
    bind(transport, thread)

    await transport.post_thread_message(thread_id="555", content=oversized("-# aq-dig:abc"))

    [content] = thread.sent
    assert len(content) <= MAX_CONTENT_CHARS
    assert content.endswith("\n-# aq-dig:abc")
    assert TRUNCATION_NOTICE in content


async def test_edit_root_fits_an_oversized_edit_and_keeps_its_marker_line():
    transport, _ = make_transport()
    channel = RecordingChannel()
    bind(transport, channel)

    await transport.edit_root(
        channel_id="424242424242424242",
        root_message_id="77",
        content=oversized("-# escalation `e` · aq-esc:k"),
    )

    [content] = channel.message.edits
    assert len(content) <= MAX_CONTENT_CHARS
    assert content.endswith("\n-# escalation `e` · aq-esc:k")


def test_content_that_fits_is_sent_unchanged():
    content = "\n".join(["heading", "y" * 1500, "https://aq.example.test/reviews/r"])
    assert bound_content(content) == content
    exact = "z" * MAX_CONTENT_CHARS
    assert bound_content(exact) == exact


def test_the_body_is_cut_from_its_end_so_the_heading_and_last_line_survive():
    link = "https://aq.example.test/reviews/rev-bright-ridge"
    content = "\n".join(
        ["📄 Revised plan (rev 2): Knowledge records", "Project: agent-queue", "n" * 5000, link]
    )

    bounded = bound_content(content)

    assert len(bounded) <= MAX_CONTENT_CHARS
    assert bounded.startswith("📄 Revised plan (rev 2): Knowledge records\nProject: agent-queue\nn")
    assert bounded.endswith(f"n{TRUNCATION_NOTICE}\n{link}")


def test_astral_characters_count_twice_so_the_bound_holds_however_discord_counts():
    bounded = bound_content("\n".join(["📄" * 1500, "tail"]))
    assert len(bounded.encode("utf-16-le")) // 2 <= MAX_CONTENT_CHARS
    assert bounded.endswith(f"{TRUNCATION_NOTICE}\ntail")


def test_a_cut_inside_a_code_block_closes_it_so_the_last_line_stays_live():
    """An unclosed fence would swallow the notice and render the link as code."""
    link = "https://aq.example.test/reviews/r"
    bounded = bound_content("\n".join(["notes:", "```", "log line\n" * 600, link]))

    assert len(bounded) <= MAX_CONTENT_CHARS
    assert bounded.count("```") % 2 == 0
    assert bounded.endswith(f"```\n{TRUNCATION_NOTICE}\n{link}")


def test_a_single_oversized_line_is_cut_with_the_notice():
    bounded = bound_content("w" * 5000)
    assert len(bounded) <= MAX_CONTENT_CHARS
    assert bounded.endswith(TRUNCATION_NOTICE)


def test_an_oversized_last_line_falls_back_to_a_plain_cut():
    bounded = bound_content("\n".join(["heading", "v" * 5000]))
    assert len(bounded) <= MAX_CONTENT_CHARS
    assert bounded.startswith("heading\nv")
    assert bounded.endswith(TRUNCATION_NOTICE)
