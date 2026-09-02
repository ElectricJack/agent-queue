"""Regression coverage for the interactive CLI menu helpers."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from src.cli import menus


def test_task_creation_wizard_reprompts_required_values_and_preserves_defaults(monkeypatch):
    """Removing required-field validation or defaults would change the creation payload."""
    responses = iter(["project-1", "A task", "Description", None, "feature", "inherit"])
    monkeypatch.setattr(menus, "prompt_input", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(menus, "prompt_choice", lambda *args, **kwargs: next(responses))

    result = menus.task_creation_wizard(["project-1"])

    assert result == {
        "project_id": "project-1",
        "title": "A task",
        "description": "Description",
        "priority": 100,
        "task_type": "feature",
    }


def _wire_wizard(monkeypatch, *, integration_mode: str) -> None:
    """Drive ``task_creation_wizard``'s prompts with canned answers."""
    inputs = {
        "Project ID": "project-1",
        "Title": "A task",
        "Description": "Description",
        "Priority": "150",
    }

    def fake_prompt_choice(message, choices, default=None):
        if message == "Type":
            return "bugfix"
        assert message == "Integration mode"
        return integration_mode

    monkeypatch.setattr(menus, "prompt_input", lambda message, **kwargs: inputs[message])
    monkeypatch.setattr(menus, "prompt_choice", fake_prompt_choice)


@pytest.mark.parametrize("mode", ["pull_request", "direct"])
def test_task_creation_wizard_carries_chosen_integration_mode(monkeypatch, mode):
    """Dropping the chosen mode would make the Step 6/6 prompt cosmetic."""
    _wire_wizard(monkeypatch, integration_mode=mode)

    result = menus.task_creation_wizard(["project-1"])

    assert result is not None
    assert result["integration_mode"] == mode
    assert result["priority"] == 150
    assert result["task_type"] == "bugfix"


def test_task_creation_wizard_omits_integration_mode_when_inherit(monkeypatch):
    """``inherit`` means "let project/system policy decide" — send no override."""
    _wire_wizard(monkeypatch, integration_mode="inherit")

    result = menus.task_creation_wizard(["project-1"])

    assert result is not None
    assert "integration_mode" not in result


def test_select_and_confirm_return_value_on_choice_and_none_or_default_on_cancel(monkeypatch):
    """A menu implementation that always returns a default loses explicit choices."""
    dialog = Mock()
    dialog.run.return_value = "chosen"
    monkeypatch.setattr(menus, "radiolist_dialog", lambda **kwargs: dialog)
    assert menus.select_from_list([("chosen", "Chosen")]) == "chosen"

    dialog.run.return_value = None
    assert menus.select_from_list([("chosen", "Chosen")]) is None

    confirmation = Mock()
    confirmation.run.return_value = True
    monkeypatch.setattr(menus, "yes_no_dialog", lambda **kwargs: confirmation)
    assert menus.confirm("Continue?", default=False) is True
    confirmation.run.return_value = None
    assert menus.confirm("Continue?", default=True) is True
