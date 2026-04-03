"""Tests for AgentQueueBot channel cache methods.

Tests the in-memory channel caching layer independently of Discord API calls.
Uses lightweight mock objects for discord.TextChannel since we only need id and name.

Covers:
- update_project_channel() — single channel per project
- get_project_for_channel() reverse lookup (O(1))
- clear_project_channels() cleanup
- Stale entry removal on update
- _get_channel() with project-specific and global fallback
- _is_global_channel()
- _prepend_project_tag() static helper
"""

from dataclasses import dataclass


@dataclass
class FakeChannel:
    """Minimal mock for discord.TextChannel with just an id and name."""

    id: int
    name: str = "fake-channel"


class TestUpdateProjectChannel:
    """Tests for update_project_channel()."""

    def _make_bot_caches(self):
        """Return a minimal namespace mimicking the bot's channel caches."""

        class BotCaches:
            def __init__(self):
                self._project_channels = {}
                self._channel_to_project = {}

        return BotCaches()

    def test_update_channel(self):
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        ch = FakeChannel(id=100, name="proj-channel")

        AgentQueueBot.update_project_channel(caches, "proj-1", ch)

        assert caches._project_channels["proj-1"] is ch
        assert caches._channel_to_project[100] == "proj-1"

    def test_stale_entry_removed(self):
        """Replacing a channel clears the old reverse mapping."""
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        old_ch = FakeChannel(id=100, name="old-channel")
        new_ch = FakeChannel(id=101, name="new-channel")

        AgentQueueBot.update_project_channel(caches, "proj-1", old_ch)
        assert caches._channel_to_project[100] == "proj-1"

        AgentQueueBot.update_project_channel(caches, "proj-1", new_ch)
        assert 100 not in caches._channel_to_project
        assert caches._channel_to_project[101] == "proj-1"

    def test_same_channel_no_stale_removal(self):
        """Re-setting the same channel doesn't remove the mapping."""
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        ch = FakeChannel(id=100, name="channel")

        AgentQueueBot.update_project_channel(caches, "proj-1", ch)
        AgentQueueBot.update_project_channel(caches, "proj-1", ch)

        assert caches._channel_to_project[100] == "proj-1"


class TestGetProjectForChannel:
    """Tests for get_project_for_channel() — O(1) reverse lookup."""

    def test_finds_project_by_channel(self):
        from src.discord.bot import AgentQueueBot

        caches = type(
            "C",
            (),
            {
                "_channel_to_project": {100: "proj-1", 200: "proj-2"},
            },
        )()

        assert AgentQueueBot.get_project_for_channel(caches, 100) == "proj-1"
        assert AgentQueueBot.get_project_for_channel(caches, 200) == "proj-2"

    def test_returns_none_for_unknown_channel(self):
        from src.discord.bot import AgentQueueBot

        caches = type(
            "C",
            (),
            {
                "_channel_to_project": {100: "proj-1"},
            },
        )()

        assert AgentQueueBot.get_project_for_channel(caches, 999) is None


class TestClearProjectChannels:
    """Tests for clear_project_channels()."""

    def _make_bot_caches(self):
        class BotCaches:
            def __init__(self):
                self._project_channels = {}
                self._channel_to_project = {}
                self._notes_threads = {}
                self._channel_summaries = {}
                self._channel_locks = {}
                self._channel_buffers = {}
                self._buffer_last_access = {}
                self._summarization_tasks = {}
                self._notes_threads_path = "/dev/null"

            def _save_notes_threads_sync(self):
                pass  # No-op for tests

        return BotCaches()

    def test_clears_channel(self):
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        ch = FakeChannel(id=100)
        caches._project_channels["proj-1"] = ch
        caches._channel_to_project[100] = "proj-1"

        AgentQueueBot.clear_project_channels(caches, "proj-1")

        assert "proj-1" not in caches._project_channels
        assert 100 not in caches._channel_to_project

    def test_clears_notes_threads(self):
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        ch = FakeChannel(id=100)
        caches._project_channels["proj-1"] = ch
        caches._channel_to_project[100] = "proj-1"
        caches._notes_threads[500] = "proj-1"
        caches._notes_threads[600] = "proj-1"
        caches._notes_threads[700] = "other-project"

        AgentQueueBot.clear_project_channels(caches, "proj-1")

        assert 500 not in caches._notes_threads
        assert 600 not in caches._notes_threads
        assert caches._notes_threads[700] == "other-project"

    def test_clears_channel_locks_and_summaries(self):
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        ch = FakeChannel(id=100)
        caches._project_channels["proj-1"] = ch
        caches._channel_to_project[100] = "proj-1"
        caches._channel_summaries[100] = (999, "summary text")
        caches._channel_locks[100] = "mock-lock"

        AgentQueueBot.clear_project_channels(caches, "proj-1")

        assert 100 not in caches._channel_summaries
        assert 100 not in caches._channel_locks

    def test_safe_to_clear_nonexistent_project(self):
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()

        # Should not raise
        AgentQueueBot.clear_project_channels(caches, "nonexistent")

    def test_does_not_affect_other_projects(self):
        from src.discord.bot import AgentQueueBot

        caches = self._make_bot_caches()
        ch1 = FakeChannel(id=100)
        ch2 = FakeChannel(id=200)
        caches._project_channels["proj-1"] = ch1
        caches._project_channels["proj-2"] = ch2
        caches._channel_to_project[100] = "proj-1"
        caches._channel_to_project[200] = "proj-2"

        AgentQueueBot.clear_project_channels(caches, "proj-1")

        assert caches._project_channels["proj-2"] is ch2
        assert caches._channel_to_project[200] == "proj-2"


class TestGetChannel:
    """Tests for _get_channel() fallback logic."""

    def test_returns_project_specific_channel(self):
        from src.discord.bot import AgentQueueBot

        global_ch = FakeChannel(id=1, name="global")
        proj_ch = FakeChannel(id=100, name="proj-channel")

        caches = type(
            "C",
            (),
            {
                "_project_channels": {"proj-1": proj_ch},
                "_channel": global_ch,
            },
        )()

        result = AgentQueueBot._get_channel(caches, "proj-1")
        assert result is proj_ch

    def test_falls_back_to_global(self):
        from src.discord.bot import AgentQueueBot

        global_ch = FakeChannel(id=1, name="global")

        caches = type(
            "C",
            (),
            {
                "_project_channels": {},
                "_channel": global_ch,
            },
        )()

        result = AgentQueueBot._get_channel(caches, "proj-1")
        assert result is global_ch

    def test_no_project_returns_global(self):
        from src.discord.bot import AgentQueueBot

        global_ch = FakeChannel(id=1, name="global")

        caches = type(
            "C",
            (),
            {
                "_project_channels": {},
                "_channel": global_ch,
            },
        )()

        result = AgentQueueBot._get_channel(caches, None)
        assert result is global_ch


class TestIsGlobalChannel:
    """Tests for _is_global_channel."""

    def test_is_global_when_no_project_channel(self):
        from src.discord.bot import AgentQueueBot

        global_ch = FakeChannel(id=1, name="global")

        caches = type(
            "C",
            (),
            {
                "_channel": global_ch,
                "_project_channels": {},
            },
        )()

        assert AgentQueueBot._is_global_channel(caches, global_ch, "proj-1") is True

    def test_not_global_when_project_has_channel(self):
        from src.discord.bot import AgentQueueBot

        global_ch = FakeChannel(id=1, name="global")
        proj_ch = FakeChannel(id=100, name="proj-channel")

        caches = type(
            "C",
            (),
            {
                "_channel": global_ch,
                "_project_channels": {"proj-1": proj_ch},
            },
        )()

        assert AgentQueueBot._is_global_channel(caches, global_ch, "proj-1") is False

    def test_not_global_when_project_id_is_none(self):
        from src.discord.bot import AgentQueueBot

        global_ch = FakeChannel(id=1, name="global")

        caches = type(
            "C",
            (),
            {
                "_channel": global_ch,
                "_project_channels": {},
            },
        )()

        assert AgentQueueBot._is_global_channel(caches, global_ch, None) is False


class TestPrependProjectTag:
    """Tests for _prepend_project_tag() static method."""

    def test_prepend_tag(self):
        from src.discord.bot import AgentQueueBot

        result = AgentQueueBot._prepend_project_tag("Task started", "my-project")
        assert result == "[`my-project`] Task started"

    def test_prepend_tag_preserves_formatting(self):
        from src.discord.bot import AgentQueueBot

        result = AgentQueueBot._prepend_project_tag("**Bold** text", "p-1")
        assert result == "[`p-1`] **Bold** text"


class TestIsAuthorized:
    """Tests for _is_authorized() — user authorization guard."""

    def _make_bot_with_auth(self, authorized_users: list[str]):
        """Return a minimal namespace mimicking the bot's config for auth."""
        config = type(
            "C",
            (),
            {
                "discord": type(
                    "D",
                    (),
                    {
                        "authorized_users": authorized_users,
                    },
                )(),
            },
        )()
        return type("B", (), {"config": config})()

    def test_empty_list_allows_everyone(self):
        from src.discord.bot import AgentQueueBot

        bot = self._make_bot_with_auth([])
        assert AgentQueueBot._is_authorized(bot, 123456789) is True
        assert AgentQueueBot._is_authorized(bot, 987654321) is True

    def test_authorized_user_allowed(self):
        from src.discord.bot import AgentQueueBot

        bot = self._make_bot_with_auth(["123456789", "111222333"])
        assert AgentQueueBot._is_authorized(bot, 123456789) is True

    def test_unauthorized_user_denied(self):
        from src.discord.bot import AgentQueueBot

        bot = self._make_bot_with_auth(["123456789"])
        assert AgentQueueBot._is_authorized(bot, 987654321) is False

    def test_user_id_as_string(self):
        from src.discord.bot import AgentQueueBot

        bot = self._make_bot_with_auth(["123456789"])
        assert AgentQueueBot._is_authorized(bot, "123456789") is True
        assert AgentQueueBot._is_authorized(bot, "999999999") is False

    def test_user_id_as_int_matches_string_config(self):
        """Config stores IDs as strings; int user IDs should still match."""
        from src.discord.bot import AgentQueueBot

        bot = self._make_bot_with_auth(["123456789"])
        assert AgentQueueBot._is_authorized(bot, 123456789) is True

    def test_multiple_authorized_users(self):
        from src.discord.bot import AgentQueueBot

        bot = self._make_bot_with_auth(["111", "222", "333"])
        assert AgentQueueBot._is_authorized(bot, 111) is True
        assert AgentQueueBot._is_authorized(bot, 222) is True
        assert AgentQueueBot._is_authorized(bot, 333) is True
        assert AgentQueueBot._is_authorized(bot, 444) is False
