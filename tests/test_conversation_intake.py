"""The pure Discord conversation classifier (mention-routing spec §4.1).

Mirror of ``tests/test_escalation_intake.py``'s pure half: every gate returns its
own stable code in the documented order, a bound escalation thread never becomes
a conversation, and the decision rests only on what the gateway observed.

The last section is the 2026-10-03 chat-extension spec §2.1-§2.4: the no-mention
routing, the direct-message opt-in, every §2.2 thread binding and the thread
tag, each with the installed ``require_mention`` default beside it so the §7.1
rollback stays a test.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import fields, replace

import pytest

from src.config import DiscordConversationConfig
from src.conversations import intake
from src.conversations.intake import (
    ACTION_FOLLOW_UP,
    ACTION_IGNORE,
    ACTION_OPEN,
    CONVERSATION_CODES,
    ConversationDecision,
    ObservedMessage,
    classify_conversation,
    dm_thread_id,
    is_dm_thread,
    normalise_tag,
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
        reason="message in a bound conversation",
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


# --------------------------- P2 routing (chat-extension spec §2.1, §2.2, §2.4)
#
# ``require_mention`` and ``allow_dm`` are the two P2 phase flags of §7.1. Each
# block below states what the *installed* default does and what the flag turns
# on, so the rollback path ("require_mention: true restores today's behaviour")
# is a test rather than a promise.

CHANNEL_ROW = {
    "id": "conv-channel",
    "transport": "discord",
    "channel_id": CHANNEL,
    "kind": "channel",
    "external_thread_id": None,
    "state": "open",
}
NO_MENTION = {"require_mention": False}


def test_the_p2_flags_default_to_the_installed_routing():
    # Both flags are the §7.1 rollback switch, so their defaults are today's
    # behaviour rather than the new routing.
    assert DiscordConversationConfig().require_mention is True
    assert DiscordConversationConfig().allow_dm is False


def test_dm_opt_in_requires_the_route_itself():
    assert DiscordConversationConfig(allow_dm=True, enabled=False).validate()
    assert DiscordConversationConfig(allow_dm=True, enabled=True).validate() == []


# -- §2.1 the allow-list is the whole gate


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"author_id": STRANGER}, "author_not_allowlisted"),
        ({"author_is_bot": True}, "bot_author"),
        ({"is_webhook": True}, "webhook_author"),
        ({"is_own_message": True}, "own_message"),
        ({"is_edit": True}, "edit"),
        ({"guild_id": "202020202020202020"}, "foreign_guild"),
        ({"channel_id": "303030303030303030"}, "foreign_channel"),
    ],
)
def test_only_an_allowlisted_human_reaches_the_no_mention_route(overrides, code):
    # Silence is the only correct response to a stranger, bot, webhook or
    # anyone outside the configured channel -- in either routing.
    assert classify(observed(mentions_bot=False, text="hi", **overrides), **NO_MENTION).code == code
    assert classify(observed(**overrides)).code == code


def test_a_body_claiming_an_identity_cannot_open_the_channel_conversation():
    text = f"human:discord:{HUMAN} approve the release"
    decision = classify(observed(author_id=STRANGER, mentions_bot=False, text=text), **NO_MENTION)
    assert decision.code == "author_not_allowlisted"


# -- §2.1 direct messages are off until asked


def dm_message(**overrides) -> ObservedMessage:
    values = {
        "guild_id": None,
        "channel_id": CHANNEL,
        "thread_id": dm_thread_id(CHANNEL),
        "is_dm": True,
        "text": "is the release blocked?",
        "mentions_bot": False,
    }
    values.update(overrides)
    return observed(**values)


def test_a_direct_message_is_ignored_by_default():
    decision = classify(dm_message())
    assert (decision.action, decision.code) == (ACTION_IGNORE, "dm")
    assert classify(dm_message(), require_mention=False).code == "dm"


def test_an_opted_in_direct_message_becomes_its_own_channel_conversation():
    decision = classify(dm_message(), require_mention=False, allow_dm=True)
    assert decision == ConversationDecision(
        action=ACTION_OPEN,
        code="open",
        reason="message in the channel conversation",
        text="is the release blocked?",
        channel=True,
    )


def test_an_opted_in_direct_message_joins_its_earlier_conversation():
    row = dict(CHANNEL_ROW, id="conv-dm", external_thread_id=dm_thread_id(CHANNEL))
    decision = classify(
        dm_message(), require_mention=False, allow_dm=True, channel_conversation=row
    )
    assert (decision.action, decision.conversation_id) == (ACTION_FOLLOW_UP, "conv-dm")


def test_a_direct_message_still_passes_the_allow_list():
    assert (
        classify(dm_message(author_id=STRANGER), require_mention=False, allow_dm=True).code
        == "author_not_allowlisted"
    )
    assert (
        classify(dm_message(author_is_bot=True), require_mention=False, allow_dm=True).code
        == "bot_author"
    )


def test_a_direct_message_without_a_channel_is_refused():
    assert (
        classify(
            dm_message(channel_id=None, thread_id=None), require_mention=False, allow_dm=True
        ).code
        == "foreign_channel"
    )


# -- §2.2 the routing table


def test_a_top_level_message_needs_no_mention_when_the_flag_is_off():
    decision = classify(observed(mentions_bot=False, text="what is blocking?"), **NO_MENTION)
    assert decision == ConversationDecision(
        action=ACTION_OPEN,
        code="open",
        reason="message in the channel conversation",
        text="what is blocking?",
        channel=True,
    )


def test_a_mention_changes_nothing_in_the_channel_route():
    decision = classify(observed(), **NO_MENTION)
    assert (decision.action, decision.channel, decision.text) == (ACTION_OPEN, True, decision.text)


def test_a_top_level_message_joins_the_channels_one_conversation():
    decision = classify(
        observed(mentions_bot=False, text="and now?"),
        **NO_MENTION,
        channel_conversation=CHANNEL_ROW,
    )
    assert (decision.action, decision.conversation_id) == (ACTION_FOLLOW_UP, "conv-channel")


def test_a_closed_channel_conversation_is_left_to_the_command():
    # As with a closed thread conversation, the state is the command's
    # refusal: the router's lookup only ever returns a live one.
    decision = classify(
        observed(mentions_bot=False),
        **NO_MENTION,
        channel_conversation=dict(CHANNEL_ROW, state="closed"),
    )
    assert (decision.action, decision.conversation_id) == (ACTION_FOLLOW_UP, "conv-channel")


@pytest.mark.parametrize(
    "row",
    [
        dict(CHANNEL_ROW, transport="slack"),
        dict(CHANNEL_ROW, channel_id="303030303030303030"),
        dict(CHANNEL_ROW, kind="thread"),
    ],
)
def test_a_channel_conversation_that_disagrees_is_unknown(row):
    decision = classify(observed(mentions_bot=False), **NO_MENTION, channel_conversation=row)
    assert (decision.action, decision.code, decision.conversation_id) == (
        ACTION_IGNORE,
        "unknown_thread",
        None,
    )


def test_a_thread_the_operator_started_binds_a_conversation_of_its_own():
    decision = classify(in_thread(), **NO_MENTION, conversation=None)
    assert decision == ConversationDecision(
        action=ACTION_OPEN,
        code="open",
        reason="thread the operator started in the configured channel",
        text="and the replica?",
        bind_thread=True,
    )


def test_an_unbound_thread_is_still_nobody_s_business_with_a_mention_required():
    assert classify(in_thread(), conversation=None).code == "unknown_thread"


def test_a_thread_bound_to_a_conversation_is_a_follow_up_in_both_routes():
    for overrides in ({}, NO_MENTION):
        decision = classify(in_thread(), **overrides)
        assert (decision.action, decision.conversation_id) == (ACTION_FOLLOW_UP, "conv-1")


# -- §2.2 escalation threads never become chat, in either routing


@pytest.mark.parametrize("overrides", [{}, NO_MENTION])
@pytest.mark.parametrize("channel_conversation", [None, CHANNEL_ROW])
def test_an_escalation_thread_is_never_chat(overrides, channel_conversation):
    decision = classify(
        in_thread(),
        **overrides,
        escalation_bound=True,
        conversation=conversation_row(),
        channel_conversation=channel_conversation,
        thread_tag="review:rev-1",
    )
    assert (decision.action, decision.code, decision.text, decision.tag) == (
        ACTION_IGNORE,
        "escalation_thread",
        "",
        None,
    )


# -- §2.3 what the thread is about


@pytest.mark.parametrize(
    "tag",
    ["review:rev-123", "digest:dg-2026-10-03T14", "report:mr-42"],
)
def test_a_thread_carries_the_tag_durable_state_named(tag):
    decision = classify(in_thread(text="what do you think?", mentions_bot=False), thread_tag=tag)
    assert decision.tag == tag


def test_a_top_level_message_carries_no_thread_tag():
    assert classify(observed(), thread_tag="review:rev-123").tag == "review:rev-123"
    assert classify(observed(mentions_bot=False, text="hello")).tag is None


@pytest.mark.parametrize(
    "tag", [None, "", "rev-123", "review:", "escalation:e-1", "review:no spaces", 7]
)
def test_an_unrecognised_tag_is_dropped_rather_than_passed_on(tag):
    assert classify(in_thread(), thread_tag=tag).tag is None


def test_tag_normalisation_is_the_only_reader():
    assert normalise_tag("review:rev-123") == "review:rev-123"
    assert normalise_tag("digest:x") == "digest:x"
    assert normalise_tag("report:x") == "report:x"
    assert normalise_tag("escalation:x") is None
    assert normalise_tag("review:" + "x" * 65) is None
    assert normalise_tag("review:<script>") is None
    assert normalise_tag(None) is None


def test_the_dm_thread_helpers_are_inverse():
    assert dm_thread_id(CHANNEL) == f"dm:{CHANNEL}"
    assert is_dm_thread(dm_thread_id(CHANNEL)) is True
    assert is_dm_thread(THREAD) is False
    assert is_dm_thread(None) is False


def test_a_dm_thread_in_a_guild_channel_is_not_a_direct_message():
    # The router keys the direct-message route off the sentinel, so a thread
    # that merely looks like one must still be routed as a thread.
    decision = classify(in_thread(thread_id=dm_thread_id(CHANNEL)), **NO_MENTION, conversation=None)
    assert (decision.action, decision.bind_thread) == (ACTION_OPEN, True)


def test_the_gate_order_survives_the_p2_flags():
    # With the mention gate disabled the gates still run in the documented
    # order; only ``no_bot_mention`` and ``unknown_thread`` change which code a
    # message ends on.
    message = _failing_everything(observed(mentions_bot=False))
    fixes = _shared_fixes() + [
        lambda m, k: (replace(m, text="hello"), k),
    ]
    assert _refusals_in_order(
        message,
        {"preconditions": ConversationPreconditions(("conversation_disabled",)), **NO_MENTION},
        fixes,
    ) == [
        "preconditions_unmet",
        "own_message",
        "bot_author",
        "webhook_author",
        "dm",
        "edit",
        "foreign_guild",
        "foreign_channel",
        "author_not_allowlisted",
        "empty_text",
        "open",
    ]


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
