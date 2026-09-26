"""Explicit replies are bounded, mention-free and link-filtered."""

from src.conversations.render import conversation_pointer, render_reply

BASE = "https://dashboard.example"
CONVERSATION = "conv-test"
KEY = "conv-reply:msg-test"
MARKER = f"(aq-conv:{KEY})"


def render(text, base_url=BASE):
    return render_reply(text, dedup_key=KEY, base_url=base_url, conversation_id=CONVERSATION)


def test_short_reply_render_is_unchanged_except_marker():
    assert render("Hello supervisor") == f"Hello supervisor {MARKER}"


def test_reply_render_neutralises_mentions_backticks_and_controls():
    assert render("<@123> <@!456> <@&789> <#123> @everyone @here `ok`\x00\x1b[31m") == (
        f"@\u200beveryone @\u200bhere ok {MARKER}"
    )


def test_reply_render_keeps_dashboard_links_and_removes_foreign_links():
    text = f"[good]({BASE}/tasks/one) https://foreign.example/x {BASE}.evil/x"
    assert render(text) == f"[good]({BASE}/tasks/one) [link removed] [link removed] {MARKER}"


def test_reply_render_without_dashboard_url_removes_all_links():
    assert render("https://foreign.example/x", base_url="") == f"[link removed] {MARKER}"


def test_long_reply_render_includes_pointer_and_marker_inside_limit():
    result = render("é" * 5000)
    assert len(result) == 1900
    assert result.endswith(f" … {conversation_pointer(BASE, CONVERSATION)} {MARKER}")


def test_reply_render_does_not_split_a_link_at_the_length_boundary():
    result = render("x" * 1750 + " " + BASE + "/" + "y" * 500)
    assert len(result) <= 1900
    assert result.endswith(f" … {conversation_pointer(BASE, CONVERSATION)} {MARKER}")
    assert BASE + "/" not in result.removesuffix(
        f" … {conversation_pointer(BASE, CONVERSATION)} {MARKER}"
    )
