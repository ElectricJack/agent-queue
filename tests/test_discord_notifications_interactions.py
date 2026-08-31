"""Interactive Discord notification views — state-changing controls
(approved plan item 20).

``TaskFailedView``'s Retry button drives the real ``restart_task`` command;
a double click (the second interaction racing in before Discord disables
the buttons) must be idempotent: exactly one restart is applied, and the
second click gets an ephemeral already-resolved reply without reaching the
command handler again.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models import Project, Task, TaskStatus


def _make_interaction():
    interaction = MagicMock()
    interaction.user = MagicMock(id=42)
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    interaction.message = MagicMock()
    interaction.message.edit = AsyncMock()
    return interaction


class _RecordingHandler:
    """Wraps a real CommandHandler, recording every execute call."""

    def __init__(self, handler):
        self._handler = handler
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, cmd: str, args: dict) -> dict:
        self.calls.append((cmd, dict(args)))
        return await self._handler.execute(cmd, args)


@pytest.mark.asyncio
async def test_task_failed_retry_double_click_is_idempotent_and_second_click_is_ephemeral(
    command_handler_factory,
):
    from src.discord.notifications import TaskFailedView

    inner = await command_handler_factory()
    db = inner._db
    await db.create_project(Project(id="p1", name="p1"))
    await db.create_task(
        Task(
            id="t-fail",
            project_id="p1",
            title="broken",
            description="d",
            status=TaskStatus.FAILED,
        )
    )

    handler = _RecordingHandler(inner)
    view = TaskFailedView("t-fail", handler=handler)
    retry_btn = next(c for c in view.children if getattr(c, "label", None) == "Retry Task")

    # First click: the real restart_task runs, buttons disable, view edits.
    first = _make_interaction()
    await retry_btn.callback(first)

    assert [cmd for cmd, _ in handler.calls] == ["restart_task"]
    assert handler.calls[0][1] == {"task_id": "t-fail"}
    task = await db.get_task("t-fail")
    assert task.status == TaskStatus.READY
    first.followup.send.assert_awaited_once()
    assert first.followup.send.await_args.kwargs.get("ephemeral") is True
    assert "restarted" in first.followup.send.await_args.args[0]
    assert all(getattr(c, "disabled", False) for c in view.children)
    first.message.edit.assert_awaited_once_with(view=view)

    # Second click (raced in before the disable landed client-side): no
    # second command execution, just an ephemeral already-resolved reply.
    second = _make_interaction()
    await retry_btn.callback(second)

    assert [cmd for cmd, _ in handler.calls] == ["restart_task"]  # still one
    second.response.defer.assert_not_awaited()
    second.followup.send.assert_not_awaited()
    second.response.send_message.assert_awaited_once()
    args, kwargs = second.response.send_message.await_args
    assert "already" in args[0].lower()
    assert kwargs.get("ephemeral") is True

    # The task was not touched again.
    task = await db.get_task("t-fail")
    assert task.status == TaskStatus.READY

    # The Skip button shares the resolved guard: it also refuses.
    skip_btn = next(c for c in view.children if getattr(c, "label", None) == "Skip Task")
    third = _make_interaction()
    await skip_btn.callback(third)
    assert [cmd for cmd, _ in handler.calls] == ["restart_task"]
    third.response.send_message.assert_awaited_once()
