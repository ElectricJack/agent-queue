"""Explicit replies are bounded, mention-free and preserve links."""

import pytest

from src.conversations.render import conversation_pointer, render_reply
from src.delivery.message import invisible

BASE = "https://dashboard.example"
CONVERSATION = "conv-test"
KEY = "conv-reply:msg-test"
MARKER = invisible(f"aq-conv:{KEY}")


def render(text, base_url=BASE):
    return render_reply(text, dedup_key=KEY, base_url=base_url, conversation_id=CONVERSATION)


def test_short_reply_render_is_unchanged_except_marker():
    assert render("Hello supervisor") == f"Hello supervisor{MARKER}"
    assert set(MARKER) <= {"\u2063", "\u200b", "\u200c", "\u200d", "\u2060"}
    assert len(MARKER) == 34
    assert "aq-conv:" not in render("Hello supervisor")


def test_reply_render_neutralises_mentions_backticks_and_controls():
    assert render("<@123> <@!456> <@&789> <#123> @everyone @here `ok`\x00\x1b[31m") == (
        f"@\u200beveryone @\u200bhere ok{MARKER}"
    )


def test_reply_render_keeps_dashboard_and_external_links():
    text = f"[good]({BASE}/tasks/one) https://foreign.example/x {BASE}.evil/x"
    assert (
        render(text)
        == f"[good]({BASE}/tasks/one) [Link](https://foreign.example/x) [Link]({BASE}.evil/x){MARKER}"
    )


def test_reply_render_without_dashboard_url_keeps_external_links():
    assert (
        render("https://foreign.example/x", base_url="")
        == f"[Link](https://foreign.example/x){MARKER}"
    )


def test_long_reply_render_includes_pointer_and_marker_inside_limit():
    result = render("é" * 5000)
    assert len(result) == 1900
    assert result.endswith(f" … {conversation_pointer(BASE, CONVERSATION)}{MARKER}")


def test_reply_render_does_not_split_a_link_at_the_length_boundary():
    suffix = f" … {conversation_pointer(BASE, CONVERSATION)}{MARKER}"
    prefix = "x" * (1900 - len(suffix) - 10)
    result = render(prefix + " " + BASE + "/" + "y" * 500)
    assert len(result) <= 1900
    assert f"[Link]({BASE}/{'y' * 500})" in result
    assert result.endswith(suffix)


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "Read https://github.com/org/repo/pull/42.",
            "Read [Link](https://github.com/org/repo/pull/42).",
        ),
        (
            "[Docs](https://example.com/a_(b)?q=@jack)",
            "[Docs](https://example.com/a_%28b%29?q=@jack)",
        ),
        ("`http://100.99.1.2:8082/tasks/t`", "[Link](http://100.99.1.2:8082/tasks/t)"),
        ("<http://queue:8082/tasks/a b>", "[Link](http://queue:8082/tasks/a%20b)"),
    ],
)
def test_authored_links_remain_clickable_without_a_dashboard_origin(text, expected):
    assert render(text, base_url="") == expected + MARKER


def test_a_link_after_the_truncated_prose_is_preserved_whole():
    result = render("x" * 5000 + " https://github.com/org/repo/pull/42")
    assert "[Link](https://github.com/org/repo/pull/42)" in result
    assert len(result) <= 1900
    assert result.endswith(f"{conversation_pointer(BASE, CONVERSATION)}{MARKER}")


def test_long_reply_with_no_dashboard_does_not_invent_a_relative_pointer():
    result = render("x" * 5000, base_url="")
    assert "dashboard link could not be built" in result
    assert "/conversations" not in result
    assert len(result) <= 1900


@pytest.mark.parametrize("prefix", ["See ", "Prose " * 1000])
def test_an_oversized_link_is_explained_instead_of_partially_posted(prefix):
    result = render(prefix + "https://example.com/" + "x" * 3000)
    assert "links could not fit" in result
    assert "https://example.com/" not in result
    assert len(result) <= 1900


def test_emoji_reply_keeps_complete_links_and_marker_within_transport_limit():
    result = render("😀" * 1500 + " https://github.com/org/repo/pull/42")
    assert len(result.encode("utf-16-le")) // 2 <= 1900
    assert "[Link](https://github.com/org/repo/pull/42)" in result
    assert result.endswith(MARKER)
