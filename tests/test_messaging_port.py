"""Tests for the messaging abstraction layer (Phase 1).

Covers:
- RichNotification construction and field validation
- NotificationAction defaults and field population
- MessagingPort ABC contract enforcement
- Type alias imports from src.messaging
"""

from __future__ import annotations

import pytest

from src.messaging import (
    MessagingPort,
    RichNotification,
    NotificationAction,
    NotifyCallback,
    ThreadSendCallback,
    CreateThreadCallback,
)
from src.messaging.port import NOTIFICATION_COLORS


# ---------------------------------------------------------------------------
# RichNotification
# ---------------------------------------------------------------------------


class TestRichNotification:
    """RichNotification construction and validation."""

    def test_minimal_construction(self):
        n = RichNotification(title="Hello")
        assert n.title == "Hello"
        assert n.description == ""
        assert n.color == "default"
        assert n.fields == []
        assert n.footer == ""
        assert n.url == ""
        assert n.actions == []

    def test_full_construction(self):
        action = NotificationAction(label="Retry", action_id="retry_task")
        n = RichNotification(
            title="Task Failed",
            description="Something went wrong",
            color="error",
            fields=[("Task ID", "`abc`", True), ("Project", "`proj`", True)],
            footer="Use /help for more info",
            url="https://example.com",
            actions=[action],
        )
        assert n.title == "Task Failed"
        assert n.description == "Something went wrong"
        assert n.color == "error"
        assert len(n.fields) == 2
        assert n.fields[0] == ("Task ID", "`abc`", True)
        assert n.footer == "Use /help for more info"
        assert n.url == "https://example.com"
        assert len(n.actions) == 1
        assert n.actions[0].action_id == "retry_task"

    def test_all_valid_colors(self):
        for color in NOTIFICATION_COLORS:
            n = RichNotification(title="test", color=color)
            assert n.color == color

    def test_invalid_color_raises(self):
        with pytest.raises(ValueError, match="Invalid notification color"):
            RichNotification(title="test", color="rainbow")

    def test_fields_are_mutable_list(self):
        n = RichNotification(title="test")
        n.fields.append(("Key", "Value", False))
        assert len(n.fields) == 1

    def test_actions_are_mutable_list(self):
        n = RichNotification(title="test")
        n.actions.append(NotificationAction(label="X", action_id="x"))
        assert len(n.actions) == 1

    def test_default_factory_isolation(self):
        """Each instance gets its own fields/actions list."""
        n1 = RichNotification(title="a")
        n2 = RichNotification(title="b")
        n1.fields.append(("x", "y", True))
        assert len(n2.fields) == 0


# ---------------------------------------------------------------------------
# NotificationAction
# ---------------------------------------------------------------------------


class TestNotificationAction:
    def test_defaults(self):
        a = NotificationAction(label="OK", action_id="confirm")
        assert a.label == "OK"
        assert a.action_id == "confirm"
        assert a.style == "primary"
        assert a.args == {}

    def test_custom_style_and_args(self):
        a = NotificationAction(
            label="Delete",
            action_id="delete_task",
            style="danger",
            args={"task_id": "abc-123"},
        )
        assert a.style == "danger"
        assert a.args["task_id"] == "abc-123"

    def test_args_isolation(self):
        a1 = NotificationAction(label="a", action_id="a")
        a2 = NotificationAction(label="b", action_id="b")
        a1.args["x"] = 1
        assert "x" not in a2.args


# ---------------------------------------------------------------------------
# MessagingPort ABC
# ---------------------------------------------------------------------------


class TestMessagingPortABC:
    """Verify that MessagingPort cannot be instantiated directly and that
    concrete subclasses must implement all abstract methods."""

    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            MessagingPort()  # type: ignore[abstract]

    def test_incomplete_subclass_raises(self):
        class Incomplete(MessagingPort):
            pass

        with pytest.raises(TypeError):
            Incomplete()  # type: ignore[abstract]

    def test_complete_subclass_instantiates(self):
        """A subclass implementing all abstract methods can be created."""

        class DummyTransport(MessagingPort):
            async def start(self) -> None:
                pass

            async def stop(self) -> None:
                pass

            async def wait_until_ready(self) -> None:
                pass

            async def send_message(self, text, project_id=None, *, notification=None):
                return None

            async def create_thread(
                self, thread_name, initial_message=None, project_id=None, task_id=None
            ):
                return None

            def set_command_handler(self, handler):
                pass

            def set_supervisor(self, supervisor):
                pass

            def get_notify_callback(self):
                return None

            def get_create_thread_callback(self):
                return None

        transport = DummyTransport()
        assert isinstance(transport, MessagingPort)


# ---------------------------------------------------------------------------
# Type alias imports
# ---------------------------------------------------------------------------


class TestTypeAliasImports:
    """Verify that the callback type aliases are importable from src.messaging."""

    def test_notify_callback_importable(self):
        assert NotifyCallback is not None

    def test_thread_send_callback_importable(self):
        assert ThreadSendCallback is not None

    def test_create_thread_callback_importable(self):
        assert CreateThreadCallback is not None


# ---------------------------------------------------------------------------
# Orchestrator type alias backward compatibility
# ---------------------------------------------------------------------------


class TestOrchestratorTypeAliasCompat:
    """Verify that the orchestrator still exports the same type aliases."""

    def test_orchestrator_exports_notify_callback(self):
        from src.orchestrator import NotifyCallback as OrcNotify
        from src.messaging.types import NotifyCallback as MsgNotify

        assert OrcNotify is MsgNotify

    def test_orchestrator_exports_thread_send_callback(self):
        from src.orchestrator import ThreadSendCallback as OrcThread
        from src.messaging.types import ThreadSendCallback as MsgThread

        assert OrcThread is MsgThread

    def test_orchestrator_exports_create_thread_callback(self):
        from src.orchestrator import CreateThreadCallback as OrcCreate
        from src.messaging.types import CreateThreadCallback as MsgCreate

        assert OrcCreate is MsgCreate
