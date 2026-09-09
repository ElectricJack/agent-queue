"""Validated single-channel settings and schedule generations (§9).

The bounds asserted here are the spec's implementation defaults: interval
15-1440 minutes, catch-up 1-168 hours, Discord IDs rather than channel names,
and digest/escalation enablement that move independently.
"""

from __future__ import annotations

import pytest

from src.config import (
    DiscordConfig,
    DiscordDigestConfig,
    DiscordEscalationConfig,
    is_discord_snowflake,
)
from src.digest.schedule import (
    config_generation,
    destination_id,
    schedule_for,
    validate_settings,
)

CHANNEL = "123456789012345678"


def base(**kwargs) -> DiscordConfig:
    kwargs.setdefault("channel_id", CHANNEL)
    return DiscordConfig(bot_token="t", guild_id="1", **kwargs)


def messages(config: DiscordConfig) -> list[str]:
    return [f"{e.section}.{e.field}: {e.message}" for e in config.validate()]


class TestDefaults:
    def test_shipped_defaults_match_the_spec(self):
        config = base()
        assert config.digest.enabled is True
        assert config.digest.interval_minutes == 60
        assert config.digest.project_ids == []
        assert config.digest.categories == ["work", "vcs", "budget", "system"]
        assert config.digest.catchup_hours == 24
        assert config.escalation.enabled is True
        assert config.escalation.mention_user_ids == []
        assert config.escalation.mention_role_ids == []
        assert config.escalation.reminder_minutes == 0
        assert config.escalation.supervisor_delivery_timeout_minutes == 15
        assert config.validate() == []

    def test_enablement_is_independent(self):
        digest_only = base(escalation=DiscordEscalationConfig(enabled=False))
        assert digest_only.digest.enabled is True
        assert digest_only.validate() == []
        escalation_only = base(digest=DiscordDigestConfig(enabled=False))
        assert escalation_only.escalation.enabled is True
        assert escalation_only.validate() == []

    def test_disabled_external_escalation_warns_without_failing(self):
        config = base(escalation=DiscordEscalationConfig(enabled=False))
        assert config.validate() == []
        assert any("still created" in note for note in config.warnings())


class TestValidation:
    @pytest.mark.parametrize("minutes", [0, 14, 1441, 100_000])
    def test_interval_outside_15_to_1440_is_rejected(self, minutes):
        errors = messages(base(digest=DiscordDigestConfig(interval_minutes=minutes)))
        assert any("interval_minutes must be between 15 and 1440" in e for e in errors)

    @pytest.mark.parametrize("minutes", [15, 60, 1440])
    def test_interval_inside_the_bounds_is_accepted(self, minutes):
        assert base(digest=DiscordDigestConfig(interval_minutes=minutes)).validate() == []

    @pytest.mark.parametrize("hours", [0, 169])
    def test_catchup_outside_1_to_168_hours_is_rejected(self, hours):
        errors = messages(base(digest=DiscordDigestConfig(catchup_hours=hours)))
        assert any("catchup_hours must be between 1 and 168" in e for e in errors)

    def test_unknown_category_names_the_valid_vocabulary(self):
        errors = messages(base(digest=DiscordDigestConfig(categories=["work", "gossip"])))
        assert any("unknown digest categories ['gossip']" in e for e in errors)
        assert any("'work', 'vcs', 'budget', 'system'" in e for e in errors)

    def test_an_empty_category_set_is_rejected_as_unsendable(self):
        errors = messages(base(digest=DiscordDigestConfig(categories=[])))
        assert any("at least one category" in e for e in errors)

    def test_a_channel_name_is_not_a_channel_id(self):
        errors = messages(base(channel_id="agent-queue"))
        assert any("must be a Discord channel ID (17-20 digits)" in e for e in errors)

    def test_an_unconfigured_channel_warns_but_never_blocks_boot(self):
        # An install must start before its channel is chosen; the panel says so.
        enabled = DiscordConfig(bot_token="t", guild_id="1")
        assert enabled.validate() == []
        assert any("No channel_id is configured" in note for note in enabled.warnings())
        off = DiscordConfig(
            bot_token="t",
            guild_id="1",
            digest=DiscordDigestConfig(enabled=False),
            escalation=DiscordEscalationConfig(enabled=False),
        )
        assert off.validate() == []

    def test_mention_ids_must_be_snowflakes(self):
        errors = messages(
            base(escalation=DiscordEscalationConfig(mention_user_ids=["@jack"], mention_role_ids=["7"]))
        )
        assert any("mention_user_ids entries must be Discord IDs" in e for e in errors)
        assert any("mention_role_ids entries must be Discord IDs" in e for e in errors)

    def test_reminder_minutes_is_zero_or_a_bounded_value(self):
        assert base(escalation=DiscordEscalationConfig(reminder_minutes=0)).validate() == []
        assert base(escalation=DiscordEscalationConfig(reminder_minutes=30)).validate() == []
        errors = messages(base(escalation=DiscordEscalationConfig(reminder_minutes=2)))
        assert any("reminder_minutes must be 0 (disabled)" in e for e in errors)

    def test_supervisor_delivery_timeout_is_bounded(self):
        errors = messages(
            base(escalation=DiscordEscalationConfig(supervisor_delivery_timeout_minutes=0))
        )
        assert any("supervisor_delivery_timeout_minutes must be between 1 and 1440" in e for e in errors)

    @pytest.mark.parametrize("value,ok", [
        ("123456789012345678", True), ("12345678901234567890", True),
        ("1234567890123456", False), ("agent-queue", False), ("<@123456789012345678>", False),
        (123456789012345678, False), (None, False),
    ])
    def test_snowflake_recogniser(self, value, ok):
        assert is_discord_snowflake(value) is ok

    def test_project_membership_is_validated_against_the_real_projects(self):
        config = base(digest=DiscordDigestConfig(project_ids=["agent-queue", "ghost"]))
        errors = validate_settings(config, frozenset({"agent-queue"}))
        assert any("unknown project 'ghost'" in e for e in errors)
        assert not any("agent-queue'" in e for e in errors)
        assert validate_settings(config, frozenset({"agent-queue", "ghost"})) == []


class TestLoading:
    def test_yaml_round_trips_into_the_nested_settings(self, tmp_path):
        import yaml

        from src.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "database": {"url": "postgresql://u:p@localhost/db"},
                    "discord": {
                        "bot_token": "t",
                        "guild_id": "1",
                        "channel_id": CHANNEL,
                        "digest": {
                            "enabled": False,
                            "interval_minutes": 120,
                            "project_ids": ["agent-queue"],
                            "categories": ["work"],
                            "catchup_hours": 6,
                        },
                        "escalation": {
                            "mention_user_ids": ["223456789012345678"],
                            "reminder_minutes": 45,
                            "supervisor_delivery_timeout_minutes": 30,
                        },
                    }
                }
            )
        )
        config = load_config(str(path))
        assert config.discord.channel_id == CHANNEL
        assert config.discord.digest.enabled is False
        assert config.discord.digest.interval_minutes == 120
        assert config.discord.digest.project_ids == ["agent-queue"]
        assert config.discord.digest.categories == ["work"]
        assert config.discord.digest.catchup_hours == 6
        assert config.discord.escalation.enabled is True
        assert config.discord.escalation.mention_user_ids == ["223456789012345678"]
        assert config.discord.escalation.reminder_minutes == 45
        assert config.discord.escalation.supervisor_delivery_timeout_minutes == 30

    def test_an_absent_discord_section_keeps_the_defaults(self, tmp_path):
        import yaml

        from src.config import load_config

        path = tmp_path / "config.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "messaging_platform": "none",
                    "database": {"url": "postgresql://u:p@localhost/db"},
                }
            )
        )
        config = load_config(str(path))
        assert config.discord.digest.interval_minutes == 60
        assert config.discord.escalation.supervisor_delivery_timeout_minutes == 15

    def test_the_editor_schema_publishes_the_bounds(self):
        from src.config_editor import build_config_schema

        discord = build_config_schema()["properties"]["discord"]["properties"]
        interval = discord["digest"]["properties"]["interval_minutes"]
        assert (interval["minimum"], interval["maximum"]) == (15, 1440)
        catchup = discord["digest"]["properties"]["catchup_hours"]
        assert (catchup["minimum"], catchup["maximum"]) == (1, 168)
        assert discord["digest"]["properties"]["categories"]["items"]["enum"] == [
            "work", "vcs", "budget", "system",
        ]
        assert discord["escalation"]["properties"]["enabled"]["default"] is True
        assert discord["channel_id"]["default"] == ""


class TestScheduleGeneration:
    def test_destination_is_the_channel_id(self):
        assert destination_id(base()) == f"discord:{CHANNEL}"
        assert destination_id(DiscordConfig()) == "discord:unconfigured"

    def test_generation_is_stable_for_unchanged_settings(self):
        assert config_generation(base()) == config_generation(base())
        assert 0 <= config_generation(base()) <= 0x7FFFFFFF

    @pytest.mark.parametrize("changed", [
        DiscordDigestConfig(interval_minutes=30),
        DiscordDigestConfig(project_ids=["agent-queue"]),
        DiscordDigestConfig(categories=["work"]),
        DiscordDigestConfig(catchup_hours=6),
    ])
    def test_a_window_defining_change_starts_a_new_generation(self, changed):
        assert config_generation(base(digest=changed)) != config_generation(base())

    def test_project_order_does_not_change_the_generation(self):
        one = base(digest=DiscordDigestConfig(project_ids=["a", "b"]))
        other = base(digest=DiscordDigestConfig(project_ids=["b", "a"]))
        assert config_generation(one) == config_generation(other)

    def test_mentions_do_not_roll_the_digest_generation(self):
        mentions = base(escalation=DiscordEscalationConfig(mention_user_ids=["223456789012345678"]))
        assert config_generation(mentions) == config_generation(base())

    def test_a_new_channel_is_a_new_destination_and_generation(self):
        other = base(channel_id="223456789012345678")
        assert destination_id(other) != destination_id(base())
        assert config_generation(other) != config_generation(base())


class TestWindows:
    def test_a_first_window_is_one_interval_long(self):
        window = schedule_for(base()).window_for(10_000.0)
        assert (window.since, window.until, window.catchup) == (10_000.0 - 3600.0, 10_000.0, False)

    def test_a_normal_window_continues_from_the_last_one(self):
        window = schedule_for(base()).window_for(10_000.0, last_window_end=8_000.0)
        assert (window.since, window.until, window.catchup) == (8_000.0, 10_000.0, False)

    def test_a_long_gap_coalesces_into_one_bounded_catchup_window(self):
        schedule = schedule_for(base(digest=DiscordDigestConfig(catchup_hours=2)))
        window = schedule.window_for(100_000.0, last_window_end=0.0)
        assert window.catchup is True
        assert window.since == 100_000.0 - 2 * 3600.0
        assert window.until == 100_000.0

    def test_next_evaluation_follows_the_configured_interval(self):
        schedule = schedule_for(base(digest=DiscordDigestConfig(interval_minutes=30)))
        assert schedule.next_evaluation_at(1_000.0, last_window_end=1_000.0) == 1_000.0 + 1800.0
        assert schedule.next_evaluation_at(1_000.0) == 1_000.0 + 1800.0
        # An overdue schedule is due now, never in the past.
        assert schedule.next_evaluation_at(9_000.0, last_window_end=1_000.0) == 9_000.0
