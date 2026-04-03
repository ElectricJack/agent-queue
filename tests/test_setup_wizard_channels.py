"""Tests for the setup wizard per-project channels step.

Tests _step_per_project_channels() by mocking user input (stdin).

Covers:
- User declines auto-create -> returns auto_create=False with defaults
- User accepts auto-create with default conventions
- Custom naming conventions
- Invalid naming convention (missing {project_id}) resets to default
- Category name configuration
- Preserves existing config values as defaults
"""

import pytest
from unittest.mock import patch
from src.setup_wizard import _step_per_project_channels


class TestStepPerProjectChannelsDecline:
    """User declines per-project channel auto-creation."""

    def test_decline_returns_auto_create_false(self):
        """When user answers 'n', auto_create is False."""
        with patch("builtins.input", return_value="n"):
            result = _step_per_project_channels({}, discord_ok=False)

        assert result["auto_create"] is False
        assert result["naming_convention"] == "{project_id}"
        assert result["category_name"] == ""

    def test_decline_preserves_existing_naming(self):
        """When declining, existing naming conventions are preserved."""
        existing = {
            "_yaml": {
                "discord": {
                    "per_project_channels": {
                        "auto_create": True,  # Was previously enabled
                        "naming_convention": "aq-{project_id}",
                        "category_name": "My Category",
                    }
                }
            }
        }
        with patch("builtins.input", return_value="n"):
            result = _step_per_project_channels(existing, discord_ok=False)

        assert result["auto_create"] is False
        # Preserves existing conventions even when declining
        assert result["naming_convention"] == "aq-{project_id}"
        assert result["category_name"] == "My Category"


class TestStepPerProjectChannelsAccept:
    """User accepts per-project channel auto-creation."""

    def test_accept_with_all_defaults(self):
        """User accepts and takes all defaults."""
        # Inputs: yes to enable, enter for convention, enter for category, enter for private
        inputs = iter(["y", "", "", ""])
        with patch("builtins.input", lambda _: next(inputs)):
            result = _step_per_project_channels({}, discord_ok=False)

        assert result["auto_create"] is True
        assert result["naming_convention"] == "{project_id}"
        assert result["category_name"] == ""

    def test_accept_with_custom_naming(self):
        """User provides custom naming convention."""
        inputs = iter(
            [
                "y",  # Enable auto-create
                "aq-{project_id}",  # Custom convention
                "Bot Channels",  # Category name
                "",  # Private (default)
            ]
        )
        with patch("builtins.input", lambda _: next(inputs)):
            result = _step_per_project_channels({}, discord_ok=False)

        assert result["auto_create"] is True
        assert result["naming_convention"] == "aq-{project_id}"
        assert result["category_name"] == "Bot Channels"

    def test_accept_no_category(self):
        """User accepts but leaves category blank."""
        inputs = iter(["y", "", "", ""])
        with patch("builtins.input", lambda _: next(inputs)):
            result = _step_per_project_channels({}, discord_ok=True)

        assert result["auto_create"] is True
        assert result["category_name"] == ""


class TestStepPerProjectChannelsValidation:
    """Invalid input handling."""

    def test_missing_project_id_in_convention_resets(self):
        """If naming convention lacks {project_id}, it resets to default."""
        inputs = iter(
            [
                "y",
                "invalid-pattern",  # Missing {project_id}
                "",  # No category
                "",  # Private (default)
            ]
        )
        with patch("builtins.input", lambda _: next(inputs)):
            result = _step_per_project_channels({}, discord_ok=False)

        assert result["naming_convention"] == "{project_id}"


class TestStepPerProjectChannelsExistingConfig:
    """Uses existing config values as defaults."""

    def test_existing_auto_create_true_as_default(self):
        """When existing config has auto_create=True, that's the default prompt."""
        existing = {
            "_yaml": {
                "discord": {
                    "per_project_channels": {
                        "auto_create": True,
                        "naming_convention": "custom-{project_id}",
                        "category_name": "Projects",
                    }
                }
            }
        }
        # User accepts everything with defaults (empty inputs)
        inputs = iter(["", "", "", ""])
        with patch("builtins.input", lambda _: next(inputs)):
            result = _step_per_project_channels(existing, discord_ok=False)

        assert result["auto_create"] is True
        assert result["naming_convention"] == "custom-{project_id}"
        assert result["category_name"] == "Projects"

    def test_empty_existing_config(self):
        """Empty existing config uses standard defaults."""
        inputs = iter(["y", "", "", ""])
        with patch("builtins.input", lambda _: next(inputs)):
            result = _step_per_project_channels({"_yaml": {}}, discord_ok=False)

        assert result["auto_create"] is True
        assert result["naming_convention"] == "{project_id}"
