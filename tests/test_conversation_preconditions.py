"""Fail-closed preconditions for Discord @mention conversations (mention-routing spec §4)."""

from src.config import AppConfig, DiscordConfig, DiscordConversationConfig
from src.conversations.preconditions import PRECONDITION_CODES, conversation_preconditions


def make_config(**discord):
    values = {"bot_token": "t", "guild_id": "1", "channel_id": "123456789012345678",
              "authorized_users": ["111111111111111111"],
              "conversation": DiscordConversationConfig(enabled=True)}
    values.update(discord)
    config = AppConfig()
    config.discord = DiscordConfig(**values)
    return config


def test_everything_set_is_ok():
    pre = conversation_preconditions(make_config(), cutover_status="complete", outbox_bound=True)
    assert pre.to_dict() == {"ok": True, "unmet": []}
    assert pre.ok


def test_the_default_config_is_off():
    assert AppConfig().discord.conversation.enabled is False
    pre = conversation_preconditions(AppConfig(), cutover_status="complete", outbox_bound=True)
    assert "conversation_disabled" in pre.unmet and not pre.ok


def test_an_empty_allowlist_fails_closed():
    pre = conversation_preconditions(
        make_config(authorized_users=[]), cutover_status="complete", outbox_bound=True
    )
    assert pre.unmet == ("empty_allowlist",)


def test_a_blank_allowlist_entry_is_not_an_allowlist():
    pre = conversation_preconditions(
        make_config(authorized_users=["", "  "]), cutover_status="complete", outbox_bound=True
    )
    assert pre.unmet == ("empty_allowlist",)


def test_each_precondition_is_named():
    config = make_config(guild_id="", channel_id="")
    config.messages.enabled = False
    config.sessions.enabled = False
    pre = conversation_preconditions(config, cutover_status="failed", outbox_bound=False)
    assert pre.unmet == ("no_guild", "no_channel", "messages_disabled", "sessions_disabled",
                         "cutover_incomplete", "outbox_unbound")


def test_an_unknown_cutover_status_is_incomplete():
    pre = conversation_preconditions(make_config(), cutover_status=None, outbox_bound=True)
    assert pre.to_dict() == {"ok": False, "unmet": ["cutover_incomplete"]}


def test_every_code_is_reported_in_the_declared_order():
    config = make_config(
        conversation=DiscordConversationConfig(enabled=False),
        authorized_users=[], guild_id="", channel_id="",
    )
    config.messages.enabled = False
    config.sessions.enabled = False
    pre = conversation_preconditions(config, cutover_status="needs_configuration",
                                     outbox_bound=False)
    assert pre.unmet == PRECONDITION_CODES


def test_enabling_with_an_empty_allowlist_is_a_config_error():
    errors = make_config(authorized_users=[]).discord.validate()
    assert any(e.field == "conversation.enabled" for e in errors)
