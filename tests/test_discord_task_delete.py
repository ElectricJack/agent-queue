"""Discord's one delete action shares preview, options, confirmation and audit."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from src.discord.task_delete import DeleteTaskView, is_delete_operator, register_task_delete
from tests.test_task_controls import env as env
from tests.test_task_removal import child

pytestmark = pytest.mark.asyncio


def bot(env):
    return SimpleNamespace(
        config=SimpleNamespace(
            discord=SimpleNamespace(
                channel_id="10",
                guild_id="20",
                authorized_users=["7"],
            )
        ),
        handler=env.handler,
        _is_authorized=lambda user: str(user) == "7",
        tree=Mock(),
    )


def interaction(user=7, channel=10):
    return SimpleNamespace(
        user=SimpleNamespace(id=user, guild_permissions=SimpleNamespace(administrator=False)),
        channel_id=channel,
        guild_id=20,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


async def test_view_previews_choices_then_deletes_once_with_verified_actor(env):
    await child(env)
    view = DeleteTaskView(bot(env), "t", 7, reason="Obsolete")
    await view.refresh()
    assert await env.db.get_task("t") is not None
    assert view.plan["task_ids"] == ["t"]
    press = interaction()
    view.scope._values = ["recursive"]
    await view.scope.callback(press)
    view.branch_choice._values = ["delete"]
    await view.branch_choice.callback(press)
    assert set(view.plan["task_ids"]) == {"t", "child"}
    assert view.plan["branches"] == "delete"
    await view.confirm.callback(press)
    await view.confirm.callback(press)
    assert await env.db.get_task("t") is None
    assert await env.db.get_task("child") is None
    audits = await env.db.get_recent_events(event_type="task.delete_requested")
    assert len(audits) == 1
    audit = json.loads(audits[0]["payload"])
    assert audit["actor"] == "human:discord:7"
    assert audit["reason"] == "Obsolete"
    assert audit["preview"]["recursive"] is True
    assert "Deleted" in press.edit_original_response.await_args.kwargs["content"]


async def test_view_rejects_other_users_and_wrong_channel_and_cancels_without_deleting(env):
    gateway = bot(env)
    view = DeleteTaskView(gateway, "t", 7)
    await view.refresh()
    assert not await view.interaction_check(interaction(user=8))
    assert not await view.interaction_check(interaction(channel=11))
    assert await view.interaction_check(interaction())
    assert not is_delete_operator(gateway, interaction(user=8))
    await view.cancel.callback(interaction())
    assert await env.db.get_task("t") is not None
    assert not await env.db.get_recent_events(event_type="task.delete_requested")


async def test_long_preview_is_complete_attachment_and_still_confirmable(env):
    view = DeleteTaskView(bot(env), "t", 7)
    await view.refresh()
    view.plan["warnings"].append({"code": "many", "message": "Long consequence. " * 300})
    response = view.response_args()
    assert len(response["content"]) < 1900
    assert len(response["attachments"]) == 1
    assert "Long consequence. " * 300 in response["attachments"][0].fp.read().decode()
    assert not view.confirm.disabled


async def test_registers_only_one_delete_command(env):
    gateway = bot(env)
    register_task_delete(gateway)
    command = gateway.tree.add_command.call_args.args[0]
    assert command.name == "delete-task"
    forbidden = interaction(user=8)
    await command.callback(forbidden, "t")
    assert forbidden.response.send_message.await_count == 1
    assert await env.db.get_task("t") is not None
