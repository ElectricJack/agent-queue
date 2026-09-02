"""Regression coverage for the interactive CLI menu helpers."""

from __future__ import annotations

from unittest.mock import Mock

from src.cli import menus


def test_task_creation_wizard_reprompts_required_values_and_preserves_defaults(monkeypatch):
    """Removing required-field validation or defaults would change the creation payload."""
    responses = iter(["project-1", "A task", "Description", None, "feature", "direct"])
    monkeypatch.setattr(menus, "prompt_input", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(menus, "prompt_choice", lambda *args, **kwargs: next(responses))

    result = menus.task_creation_wizard(["project-1"])

    assert result == {
        "project_id": "project-1",
        "title": "A task",
        "description": "Description",
        "priority": 100,
        "task_type": "feature",
        "integration_mode": "direct",
    }


def test_task_creation_wizard_includes_integration_mode_when_not_inherit(monkeypatch):
    """The wizard asks for an integration policy; dropping it silently ignores the answer."""
    for mode in ("pull_request", "direct"):
        responses = iter(["project-1", "A task", "Description", None, "feature", mode])
        monkeypatch.setattr(menus, "prompt_input", lambda *a, **k: next(responses))
        monkeypatch.setattr(menus, "prompt_choice", lambda *a, **k: next(responses))

        result = menus.task_creation_wizard(["project-1"])

        assert result["integration_mode"] == mode


def test_task_creation_wizard_omits_integration_mode_for_inherit(monkeypatch):
    """`inherit` means defer to project/system policy, so no key should be sent."""
    responses = iter(["project-1", "A task", "Description", None, "feature", "inherit"])
    monkeypatch.setattr(menus, "prompt_input", lambda *a, **k: next(responses))
    monkeypatch.setattr(menus, "prompt_choice", lambda *a, **k: next(responses))

    result = menus.task_creation_wizard(["project-1"])

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
