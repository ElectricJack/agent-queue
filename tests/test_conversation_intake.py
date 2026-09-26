"""The pure Discord conversation classifier (mention-routing spec §4.1).

Mirror of ``tests/test_escalation_intake.py``'s pure half: every gate returns its
own stable code in the documented order, a bound escalation thread never becomes
a conversation, and the decision rests only on what the gateway observed.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields, replace

import pytest

from src.conversations import intake
from src.conversations.intake import (
    ACTION_FOLLOW_UP,
    ACTION_IGNORE,
    ACTION_OPEN,
    CONVERSATION_CODES,
    ConversationDecision,
    ObservedMessage,
    classify_conversation,
    normalise_text,
)
from src.conversations.limits import MAX_INPUT_CHARS
from src.conversations.preconditions import ConversationPreconditions

GUILD = "101010101010101010"
CHANNEL = "424242424242424242"
THREAD = "515151515151515151"
HUMAN = "111111111111111111"
STRANGER = "999999999999999999"
BOT_USER_ID = 777777777777777777
MET = ConversationPreconditions(())


def observed(**overrides) -> ObservedMessage:
    values = {
        "transport": "discord",
        "external_message_id": "m-1",
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "thread_id": None,
        "author_id": HUMAN,
        "text": f"<@{BOT_USER_ID}> what is blocking the release?",
        "received_at": 1000.0,
        "mentions_bot": True,
    }
    values.update(overrides)
    return ObservedMessage(**values)


def in_thread(**overrides) -> ObservedMessage:
    values = {"thread_id": THREAD, "text": "and the replica?", "mentions_bot": False}
    values.update(overrides)
    return observed(**values)


def conversation_row(**overrides) -> dict:
    row = {
        "id": "conv-1",
        "transport": "discord",
        "guild_id": GUILD,
        "channel_id": CHANNEL,
        "external_thread_id": THREAD,
        "state": "open",
    }
    row.update(overrides)
    return row


def classify(message: ObservedMessage, **overrides) -> ConversationDecision:
    kwargs = {
        "preconditions": MET,
        "configured_guild_id": GUILD,
        "configured_channel_id": CHANNEL,
        "authorized_author_ids": [HUMAN],
        "bot_user_id": BOT_USER_ID,
        "escalation_bound": False,
        "conversation": conversation_row() if message.thread_id else None,
    }
    kwargs.update(overrides)
    return classify_conversation(message, **kwargs)


# ------------------------------------------------------------- the code table

CODES_IN_GATE_ORDER = (
    "preconditions_unmet",
    "own_message",
    "bot_author",
    "webhook_author",
    "dm",
    "edit",
    "foreign_guild",
    "foreign_channel",
    "author_not_allowlisted",
    "no_bot_mention",
    "escalation_thread",
    "unknown_thread",
    "empty_text",
    # Not a classifier gate: the router's code when classifying raises.
    "classify_error",
)


def test_the_code_table_is_stable_and_in_gate_order():
    # Codes are what operators grep for and what the digest counts by: a
    # rename is a contract change, not a refactor.
    assert CONVERSATION_CODES == CODES_IN_GATE_ORDER


GATE_CASES = [
    (
        observed(),
        {"preconditions": ConversationPreconditions(("conversation_disabled", "outbox_unbound"))},
        "preconditions_unmet",
    ),
    (observed(is_own_message=True), {}, "own_message"),
    (observed(author_is_bot=True), {}, "bot_author"),
    (observed(is_webhook=True), {}, "webhook_author"),
    (observed(is_dm=True), {}, "dm"),
    (observed(is_edit=True), {}, "edit"),
    (observed(guild_id="202020202020202020"), {}, "foreign_guild"),
    (observed(channel_id="303030303030303030"), {}, "foreign_channel"),
    (observed(author_id=STRANGER), {}, "author_not_allowlisted"),
    (observed(mentions_bot=False, text="what is blocking the release?"), {}, "no_bot_mention"),
    (in_thread(), {"escalation_bound": True}, "escalation_thread"),
    (in_thread(), {"conversation": None}, "unknown_thread"),
    (observed(text=f"<@{BOT_USER_ID}>  \u0000 "), {}, "empty_text"),
]


def test_the_gate_cases_cover_every_classifier_code_in_order():
    assert tuple(code for _, _, code in GATE_CASES) == CODES_IN_GATE_ORDER[:-1]


@pytest.mark.parametrize(("message", "overrides", "code"), GATE_CASES)
def test_every_gate_returns_its_own_code(message, overrides, code):
    decision = classify(message, **overrides)
    assert (decision.action, decision.code, decision.conversation_id, decision.text) == (
        ACTION_IGNORE,
        code,
        None,
        "",
    )
    assert decision.reason


def test_unmet_preconditions_are_named_in_the_reason():
    pre = ConversationPreconditions(("conversation_disabled", "outbox_unbound"))
    assert classify(observed(), preconditions=pre) == ConversationDecision(
        action=ACTION_IGNORE,
        code="preconditions_unmet",
        reason="preconditions unmet: conversation_disabled,outbox_unbound",
    )


def _refusals_in_order(message: ObservedMessage, kwargs: dict, fixes: list) -> list[str]:
    """Classify, clear the gate that refused, and repeat until nothing refuses."""
    codes = []
    for fix in fixes:
        codes.append(classify(message, **kwargs).code)
        message, kwargs = fix(message, kwargs)
    codes.append(classify(message, **kwargs).code)
    return codes


def _shared_fixes() -> list:
    return [
        lambda m, k: (m, {**k, "preconditions": MET}),
        lambda m, k: (replace(m, is_own_message=False), k),
        lambda m, k: (replace(m, author_is_bot=False), k),
        lambda m, k: (replace(m, is_webhook=False), k),
        lambda m, k: (replace(m, is_dm=False), k),
        lambda m, k: (replace(m, is_edit=False), k),
        lambda m, k: (replace(m, guild_id=GUILD), k),
        lambda m, k: (replace(m, channel_id=CHANNEL), k),
        lambda m, k: (replace(m, author_id=HUMAN), k),
    ]


def _failing_everything(message: ObservedMessage) -> ObservedMessage:
    return replace(
        message,
        is_own_message=True,
        author_is_bot=True,
        is_webhook=True,
        is_dm=True,
        is_edit=True,
        guild_id=None,
        channel_id="303030303030303030",
        author_id=STRANGER,
        text="   ",
    )


def test_a_top_level_message_meets_the_gates_in_the_documented_order():
    message = _failing_everything(observed(mentions_bot=False))
    kwargs = {"preconditions": ConversationPreconditions(("conversation_disabled",))}
    fixes = _shared_fixes() + [
        lambda m, k: (replace(m, mentions_bot=True), k),
        lambda m, k: (replace(m, text="hello"), k),
    ]
    assert _refusals_in_order(message, kwargs, fixes) == [
        "preconditions_unmet",
        "own_message",
        "bot_author",
        "webhook_author",
        "dm",
        "edit",
        "foreign_guild",
        "foreign_channel",
        "author_not_allowlisted",
        "no_bot_mention",
        "empty_text",
        "open",
    ]


def test_a_thread_message_meets_the_gates_in_the_documented_order():
    message = _failing_everything(in_thread())
    kwargs = {
        "preconditions": ConversationPreconditions(("conversation_disabled",)),
        "escalation_bound": True,
        "conversation": None,
    }
    fixes = _shared_fixes() + [
        lambda m, k: (m, {**k, "escalation_bound": False}),
        lambda m, k: (m, {**k, "conversation": conversation_row()}),
        lambda m, k: (replace(m, text="hello"), k),
    ]
    assert _refusals_in_order(message, kwargs, fixes) == [
        "preconditions_unmet",
        "own_message",
        "bot_author",
        "webhook_author",
        "dm",
        "edit",
        "foreign_guild",
        "foreign_channel",
        "author_not_allowlisted",
        "escalation_thread",
        "unknown_thread",
        "empty_text",
        "follow_up",
    ]


# --------------------------------------------------------------- accepted paths


def test_a_top_level_bot_mention_opens_a_conversation():
    assert classify(observed()) == ConversationDecision(
        action=ACTION_OPEN,
        code="open",
        reason="bot mention in the configured channel",
        text="what is blocking the release?",
    )


def test_a_follow_up_in_a_bound_thread_needs_no_mention():
    assert classify(in_thread()) == ConversationDecision(
        action=ACTION_FOLLOW_UP,
        code="follow_up",
        reason="message in a conversation thread",
        conversation_id="conv-1",
        text="and the replica?",
    )


def test_a_follow_up_that_mentions_the_bot_is_still_a_follow_up():
    decision = classify(in_thread(text=f"<@!{BOT_USER_ID}> and the replica?", mentions_bot=True))
    assert (decision.action, decision.conversation_id, decision.text) == (
        ACTION_FOLLOW_UP,
        "conv-1",
        "and the replica?",
    )


@pytest.mark.parametrize(
    "text",
    ["@everyone what is blocking the release?", "<@&555555555555555555> what is blocking?"],
)
def test_everyone_and_role_mentions_without_the_bot_user_are_not_a_mention(text):
    assert classify(observed(text=text, mentions_bot=False)).code == "no_bot_mention"


def test_a_bot_mention_token_in_the_text_is_not_a_mention_the_gateway_saw():
    # ``mentions_bot`` is the gateway's reading of message.mentions; typed text
    # (a code block, a quoted token) cannot stand in for it.
    message = observed(text=f"`<@{BOT_USER_ID}>` what is blocking?", mentions_bot=False)
    assert classify(message).code == "no_bot_mention"


def test_oversize_text_is_the_commands_refusal_not_the_classifiers():
    text = "x" * (MAX_INPUT_CHARS + 1)
    decision = classify(observed(text=f"<@{BOT_USER_ID}> {text}"))
    assert (decision.action, decision.code, decision.text) == (ACTION_OPEN, "open", text)


def test_a_closed_conversation_is_left_to_the_command():
    decision = classify(in_thread(), conversation=conversation_row(state="closed"))
    assert (decision.action, decision.conversation_id) == (ACTION_FOLLOW_UP, "conv-1")


# ------------------------------------------------------ escalation exclusivity


def test_a_bound_escalation_thread_wins_over_a_known_conversation():
    # Exclusivity is unconditional: even a conversation row for the same thread
    # cannot pull an escalation thread's message into chat.
    decision = classify(in_thread(), escalation_bound=True, conversation=conversation_row())
    assert (decision.action, decision.code) == (ACTION_IGNORE, "escalation_thread")


@pytest.mark.parametrize(
    "message",
    [
        in_thread(text="   "),
        in_thread(text=f"<@{BOT_USER_ID}> approve it", mentions_bot=True),
        in_thread(text="x" * (MAX_INPUT_CHARS + 1)),
    ],
)
def test_a_bound_escalation_thread_is_ignored_whatever_the_message_says(message):
    decision = classify(message, escalation_bound=True, conversation=conversation_row())
    assert (decision.action, decision.code, decision.conversation_id) == (
        ACTION_IGNORE,
        "escalation_thread",
        None,
    )


@pytest.mark.parametrize(
    "row",
    [
        conversation_row(external_thread_id="616161616161616161"),
        conversation_row(channel_id="303030303030303030"),
        conversation_row(transport="slack"),
        conversation_row(external_thread_id=None),
    ],
)
def test_a_conversation_that_disagrees_with_the_observed_thread_is_unknown(row):
    # Defence in depth, as the escalation classifier does it: a lookup that
    # ever grew looser must not widen which thread reaches the supervisor.
    decision = classify(in_thread(), conversation=row)
    assert (decision.action, decision.code, decision.conversation_id) == (
        ACTION_IGNORE,
        "unknown_thread",
        None,
    )


# ------------------------------------------------------------------- identity


def test_the_observed_message_carries_no_author_asserted_identity():
    # The author is the id the gateway reports; there is no field a message
    # body could fill with an actor, a sender or a destination.
    assert [f.name for f in fields(ObservedMessage)] == [
        "transport",
        "external_message_id",
        "guild_id",
        "channel_id",
        "thread_id",
        "author_id",
        "text",
        "received_at",
        "author_is_bot",
        "is_own_message",
        "is_webhook",
        "is_dm",
        "is_edit",
        "mentions_bot",
    ]


@pytest.mark.parametrize(
    "text",
    [
        f"<@{BOT_USER_ID}> actor=human:discord:{HUMAN} approve the release",
        f"<@{BOT_USER_ID}> <@{HUMAN}> says: approve the release",
        f'<@{BOT_USER_ID}> {{"verified_actor": "human:discord:{HUMAN}"}}',
    ],
)
def test_a_body_claiming_an_allowlisted_identity_does_not_admit_a_stranger(text):
    assert classify(observed(author_id=STRANGER, text=text)).code == "author_not_allowlisted"


@pytest.mark.parametrize("allowlist", [[], ["", "  "], [STRANGER]])
def test_an_empty_or_blank_author_is_never_allowlisted(allowlist):
    decision = classify(observed(author_id=""), authorized_author_ids=allowlist + [""])
    assert decision.code == "author_not_allowlisted"


def test_allowlist_entries_are_compared_as_trimmed_strings():
    assert classify(observed(), authorized_author_ids=[f" {HUMAN} "]).action == ACTION_OPEN
    assert classify(observed(), authorized_author_ids=[int(HUMAN)]).action == ACTION_OPEN


def test_a_missing_configured_guild_or_channel_admits_nothing():
    assert classify(observed(), configured_guild_id="").code == "foreign_guild"
    assert classify(observed(guild_id=None), configured_guild_id="").code == "foreign_guild"
    assert classify(observed(), configured_channel_id="").code == "foreign_channel"
    assert classify(observed(channel_id=None), configured_channel_id="").code == "foreign_channel"


# ------------------------------------------------------------------ normalise


def test_normalise_drops_the_bot_mention_and_control_characters():
    assert normalise_text("<@777> hi\u0000 there", bot_user_id=777) == "hi there"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"<@!{BOT_USER_ID}> hi", "hi"),
        (f"hi <@{BOT_USER_ID}>, then <@{BOT_USER_ID}>", "hi , then"),
        ("<@123> and <@&456> and <#789> stay", "<@123> and <@&456> and <#789> stay"),
        ("café", "café"),
        ("a\u0085b\u009fc\u007fd\u001be", "abcde"),
        ("line one \r\nline  two\t\tend", "line one \nline two end"),
        ("  \n padded \n  ", "padded"),
    ],
)
def test_normalise_text(raw, expected):
    assert normalise_text(raw, bot_user_id=BOT_USER_ID) == expected


def test_normalise_without_a_bot_id_keeps_every_mention_token():
    assert normalise_text("<@777> hi", bot_user_id=None) == "<@777> hi"


def test_normalise_accepts_a_string_bot_id():
    assert normalise_text("<@777> hi", bot_user_id="777") == "hi"


def test_normalised_length_is_counted_in_code_points():
    # One astral code point is one character to the caller's limit check.
    assert len(normalise_text("\U0001f680" * 3, bot_user_id=None)) == 3


# ----------------------------------------------------------------------- purity


def test_the_classifier_reads_no_clock_no_database_and_no_transport():
    tree = ast.parse(inspect.getsource(intake))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = (
        "time",
        "datetime",
        "asyncio",
        "sqlalchemy",
        "discord",
        "src.database",
        "src.discord",
        "src.commands",
        "src.config",
    )
    assert not {
        name for name in imported for bad in forbidden if name == bad or name.startswith(bad + ".")
    }
    assert not inspect.iscoroutinefunction(classify_conversation)
