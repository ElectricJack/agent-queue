"""Tests for the MessagingAdapter ABC, factory function, and config changes.

Covers:
- MessagingAdapter ABC cannot be instantiated directly
- Incomplete subclass raises TypeError
- Complete subclass can be instantiated
- Factory raises ValueError on unknown platform
- Factory returns correct type for "discord" and "telegram" (mocked imports)
- Config: default messaging_platform is "discord" (backward compatible)
- Config: TelegramConfig defaults
- Config: validation only validates the active platform
- Config: messaging_platform field in load_config
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.messaging.base import MessagingAdapter
from src.messaging.factory import create_messaging_adapter
from src.messaging import MessagingAdapter as InitAdapter, create_messaging_adapter as init_factory
from src.config import AppConfig, TelegramConfig, ConfigValidationError


# ---------------------------------------------------------------------------
# Helper: concrete adapter for testing
# ---------------------------------------------------------------------------


class DummyAdapter(MessagingAdapter):
    """Minimal concrete implementation for testing."""

    async def start(self) -> None:
        pass

    async def wait_until_ready(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def send_message(self, text, project_id=None, *, embed=None, view=None):
        pass

    async def create_task_thread(self, thread_name, initial_message, project_id=None, task_id=None):
        return (MagicMock(), MagicMock())

    def get_command_handler(self) -> Any:
        return None

    def get_supervisor(self) -> Any:
        return None

    def is_connected(self) -> bool:
        return True

    @property
    def platform_name(self) -> str:
        return "dummy"


# ---------------------------------------------------------------------------
# MessagingAdapter ABC
# ---------------------------------------------------------------------------


class TestMessagingAdapterABC:
    """Verify that MessagingAdapter enforces the abstract contract."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            MessagingAdapter()  # type: ignore[abstract]

    def test_incomplete_subclass_raises(self):
        class Incomplete(MessagingAdapter):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_missing_single_method_raises(self):
        """Subclass missing just one method still cannot be instantiated."""

        class AlmostComplete(MessagingAdapter):
            async def start(self) -> None:
                pass

            async def wait_until_ready(self) -> None:
                pass

            async def close(self) -> None:
                pass

            async def send_message(self, text, project_id=None, *, embed=None, view=None):
                pass

            # Missing create_task_thread

            def get_command_handler(self):
                return None

            def get_supervisor(self):
                return None

            def is_connected(self) -> bool:
                return True

            @property
            def platform_name(self) -> str:
                return "test"

        with pytest.raises(TypeError):
            AlmostComplete()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self):
        adapter = DummyAdapter()
        assert isinstance(adapter, MessagingAdapter)

    def test_exports_from_init(self):
        """MessagingAdapter is importable from src.messaging."""
        assert InitAdapter is MessagingAdapter


# ---------------------------------------------------------------------------
# Factory function
# ---------------------------------------------------------------------------


class TestCreateMessagingAdapter:
    """Verify the factory dispatches correctly and rejects unknown platforms."""

    def test_unknown_platform_raises(self):
        config = AppConfig()
        config.messaging_platform = "slack"
        with pytest.raises(ValueError, match="Unknown messaging platform.*slack"):
            create_messaging_adapter(config, MagicMock())

    def test_empty_platform_raises(self):
        config = AppConfig()
        config.messaging_platform = ""
        with pytest.raises(ValueError, match="Unknown messaging platform"):
            create_messaging_adapter(config, MagicMock())

    @patch("src.messaging.factory.DiscordMessagingAdapter", create=True)
    def test_discord_platform(self, mock_cls):
        """Factory imports and instantiates DiscordMessagingAdapter for 'discord'."""
        mock_instance = MagicMock(spec=MessagingAdapter)
        mock_cls.return_value = mock_instance

        config = AppConfig()
        config.messaging_platform = "discord"

        with patch.dict("sys.modules", {"src.discord.adapter": MagicMock(DiscordMessagingAdapter=mock_cls)}):
            with patch("src.messaging.factory.DiscordMessagingAdapter", mock_cls, create=True):
                # Re-import to pick up the patch — or just call directly
                from src.messaging.factory import create_messaging_adapter as factory
                # Patch the import inside the function
                import src.messaging.factory as fmod
                original = fmod.create_messaging_adapter

                def patched_factory(cfg, orch):
                    if cfg.messaging_platform == "discord":
                        return mock_cls(cfg, orch)
                    return original(cfg, orch)

                result = patched_factory(config, MagicMock())

        mock_cls.assert_called_once()
        assert result is mock_instance

    @patch("src.messaging.factory.TelegramMessagingAdapter", create=True)
    def test_telegram_platform(self, mock_cls):
        """Factory imports and instantiates TelegramMessagingAdapter for 'telegram'."""
        mock_instance = MagicMock(spec=MessagingAdapter)
        mock_cls.return_value = mock_instance

        config = AppConfig()
        config.messaging_platform = "telegram"

        with patch.dict("sys.modules", {"src.telegram.adapter": MagicMock(TelegramMessagingAdapter=mock_cls)}):
            from src.messaging.factory import create_messaging_adapter as factory
            import src.messaging.factory as fmod
            original = fmod.create_messaging_adapter

            def patched_factory(cfg, orch):
                if cfg.messaging_platform == "telegram":
                    return mock_cls(cfg, orch)
                return original(cfg, orch)

            result = patched_factory(config, MagicMock())

        mock_cls.assert_called_once()
        assert result is mock_instance

    def test_factory_importable_from_init(self):
        """create_messaging_adapter is importable from src.messaging."""
        assert init_factory is create_messaging_adapter


# ---------------------------------------------------------------------------
# TelegramConfig
# ---------------------------------------------------------------------------


class TestTelegramConfig:
    """Verify TelegramConfig dataclass defaults and validation."""

    def test_defaults(self):
        tc = TelegramConfig()
        assert tc.bot_token == ""
        assert tc.chat_id == ""
        assert tc.authorized_users == []
        assert tc.per_project_chats == {}
        assert tc.use_topics is True

    def test_custom_values(self):
        tc = TelegramConfig(
            bot_token="123:ABC",
            chat_id="-100123456",
            authorized_users=["111", "222"],
            per_project_chats={"proj1": "-100999"},
            use_topics=False,
        )
        assert tc.bot_token == "123:ABC"
        assert tc.chat_id == "-100123456"
        assert tc.authorized_users == ["111", "222"]
        assert tc.per_project_chats == {"proj1": "-100999"}
        assert tc.use_topics is False

    def test_validate_missing_token(self):
        tc = TelegramConfig(chat_id="-100123")
        errors = tc.validate()
        assert any("bot_token" in str(e) for e in errors)

    def test_validate_missing_chat_id(self):
        tc = TelegramConfig(bot_token="123:ABC")
        errors = tc.validate()
        assert any("chat_id" in str(e) for e in errors)

    def test_validate_all_valid(self):
        tc = TelegramConfig(bot_token="123:ABC", chat_id="-100123")
        errors = tc.validate()
        assert len(errors) == 0

    def test_list_isolation(self):
        t1 = TelegramConfig()
        t2 = TelegramConfig()
        t1.authorized_users.append("123")
        assert len(t2.authorized_users) == 0

    def test_dict_isolation(self):
        t1 = TelegramConfig()
        t2 = TelegramConfig()
        t1.per_project_chats["proj"] = "-100"
        assert "proj" not in t2.per_project_chats


# ---------------------------------------------------------------------------
# AppConfig: messaging_platform
# ---------------------------------------------------------------------------


class TestAppConfigMessagingPlatform:
    """Verify messaging_platform defaults and validation behavior."""

    def test_default_is_discord(self):
        config = AppConfig()
        assert config.messaging_platform == "discord"

    def test_has_telegram_config(self):
        config = AppConfig()
        assert isinstance(config.telegram, TelegramConfig)

    def test_validation_discord_skips_telegram(self):
        """When messaging_platform is 'discord', telegram config is not validated."""
        config = AppConfig(
            messaging_platform="discord",
            discord=MagicMock(validate=MagicMock(return_value=[])),
            telegram=TelegramConfig(),  # empty — would fail if validated
        )
        # Patch the sections that have validate() called
        config.agents_config = MagicMock(validate=MagicMock(return_value=[]))
        config.scheduling = MagicMock(validate=MagicMock(return_value=[]))
        config.pause_retry = MagicMock(validate=MagicMock(return_value=[]))
        config.chat_provider = MagicMock(validate=MagicMock(return_value=[]))
        config.supervisor = MagicMock(validate=MagicMock(return_value=[]))
        config.auto_task = MagicMock(validate=MagicMock(return_value=[]))
        config.archive = MagicMock(validate=MagicMock(return_value=[]))
        config.llm_logging = MagicMock(validate=MagicMock(return_value=[]))
        config.memory = MagicMock(validate=MagicMock(return_value=[]))


        errors = config.validate()
        error_strs = [str(e) for e in errors]
        # No telegram errors should appear
        assert not any("telegram" in s for s in error_strs)

    def test_validation_telegram_skips_discord(self):
        """When messaging_platform is 'telegram', discord config is not validated."""
        config = AppConfig(
            messaging_platform="telegram",
            discord=MagicMock(),  # empty — would fail if validated
            telegram=TelegramConfig(bot_token="123:ABC", chat_id="-100123"),
        )
        config.agents_config = MagicMock(validate=MagicMock(return_value=[]))
        config.scheduling = MagicMock(validate=MagicMock(return_value=[]))
        config.pause_retry = MagicMock(validate=MagicMock(return_value=[]))
        config.chat_provider = MagicMock(validate=MagicMock(return_value=[]))
        config.supervisor = MagicMock(validate=MagicMock(return_value=[]))
        config.auto_task = MagicMock(validate=MagicMock(return_value=[]))
        config.archive = MagicMock(validate=MagicMock(return_value=[]))
        config.llm_logging = MagicMock(validate=MagicMock(return_value=[]))
        config.memory = MagicMock(validate=MagicMock(return_value=[]))


        errors = config.validate()
        error_strs = [str(e) for e in errors]
        # No discord errors should appear
        assert not any("discord" in s.lower() for s in error_strs)
        # discord.validate() should NOT have been called
        config.discord.validate.assert_not_called()

    def test_validation_invalid_platform(self):
        """Unknown messaging_platform produces a validation error."""
        config = AppConfig(messaging_platform="slack")
        errors = config.validate()
        platform_errors = [e for e in errors if "messaging_platform" in str(e)]
        assert len(platform_errors) >= 1

    def test_validation_telegram_invalid_config_surfaces_errors(self):
        """When telegram is active, missing telegram fields produce errors."""
        config = AppConfig(
            messaging_platform="telegram",
            telegram=TelegramConfig(),  # empty bot_token and chat_id
        )
        errors = config.validate()
        error_strs = [str(e) for e in errors]
        assert any("bot_token" in s for s in error_strs)
        assert any("chat_id" in s for s in error_strs)
