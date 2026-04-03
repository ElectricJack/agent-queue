"""Tests for per_project_channels config loading.

Covers:
- Default PerProjectChannelsConfig values
- Loading per_project_channels from YAML
- Partial config (some fields provided, rest defaulted)
- Missing per_project_channels section uses defaults
- Custom naming conventions
- Category name parsing
"""

import pytest
import yaml
from src.config import load_config, PerProjectChannelsConfig


@pytest.fixture
def config_dir(tmp_path):
    return tmp_path


class TestPerProjectChannelsDefaults:
    """Verify default values for PerProjectChannelsConfig."""

    def test_default_values(self):
        ppc = PerProjectChannelsConfig()

        assert ppc.auto_create is False
        assert ppc.naming_convention == "{project_id}"
        assert ppc.category_name == ""

    def test_default_when_not_in_yaml(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "test-token",
                        "guild_id": "123",
                    }
                }
            )
        )
        config = load_config(str(config_file))

        ppc = config.discord.per_project_channels
        assert ppc.auto_create is False
        assert ppc.naming_convention == "{project_id}"
        assert ppc.category_name == ""


class TestPerProjectChannelsFromYAML:
    """Loading per_project_channels from YAML config."""

    def test_full_config(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "test-token",
                        "guild_id": "123",
                        "per_project_channels": {
                            "auto_create": True,
                            "naming_convention": "aq-{project_id}",
                            "category_name": "Agent Queue Projects",
                        },
                    }
                }
            )
        )
        config = load_config(str(config_file))

        ppc = config.discord.per_project_channels
        assert ppc.auto_create is True
        assert ppc.naming_convention == "aq-{project_id}"
        assert ppc.category_name == "Agent Queue Projects"

    def test_partial_config_auto_create_only(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "test-token",
                        "guild_id": "123",
                        "per_project_channels": {
                            "auto_create": True,
                        },
                    }
                }
            )
        )
        config = load_config(str(config_file))

        ppc = config.discord.per_project_channels
        assert ppc.auto_create is True
        # Other fields should use defaults
        assert ppc.naming_convention == "{project_id}"
        assert ppc.category_name == ""

    def test_partial_config_custom_naming(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "test-token",
                        "guild_id": "123",
                        "per_project_channels": {
                            "auto_create": False,
                            "naming_convention": "{project_id}-notify",
                        },
                    }
                }
            )
        )
        config = load_config(str(config_file))

        ppc = config.discord.per_project_channels
        assert ppc.auto_create is False
        assert ppc.naming_convention == "{project_id}-notify"

    def test_empty_per_project_channels_section(self, config_dir):
        config_file = config_dir / "config.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "discord": {
                        "bot_token": "test-token",
                        "guild_id": "123",
                        "per_project_channels": {},
                    }
                }
            )
        )
        config = load_config(str(config_file))

        ppc = config.discord.per_project_channels
        assert ppc.auto_create is False
        assert ppc.naming_convention == "{project_id}"


class TestNamingConventionUsage:
    """Verify naming conventions are usable with project IDs."""

    def test_default_convention_formats_correctly(self):
        ppc = PerProjectChannelsConfig()
        assert ppc.naming_convention.format(project_id="my-app") == "my-app"

    def test_custom_convention_formats_correctly(self):
        ppc = PerProjectChannelsConfig(
            naming_convention="aq-{project_id}",
        )
        assert ppc.naming_convention.format(project_id="web") == "aq-web"
