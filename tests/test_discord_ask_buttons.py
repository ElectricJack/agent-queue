"""Opaque gateway controls carry authenticated, generation-bound human evidence."""

import asyncio

import pytest
from sqlalchemy import select, update

from src.commands.principal import ExecutionPrincipal, principal_context
from src.database.tables import ask_action_tokens, ask_decisions, human_communication_modes
from tests.test_ask_delivery import binding, change_title, settle
from tests.test_ask_delivery import delivery_env as delivery_env_fixture  # noqa: F401


async def press(env, custom_id, **overrides):
    root = (await binding(env))["external_root_id"]
    args = {
        "interaction_id": "press-1",
        "custom_id": custom_id,
        "guild_id": "1",
        "channel_id": "2",
        "message_id": root,
        "author_id": "3",
        **overrides,
    }
    with principal_context(ExecutionPrincipal.service("discord-gateway")):
        return await env.handler._cmd_ask_button(args)


async def controls(env):
    await settle(env)
    return env.sink.messages[(await binding(env))["external_root_id"]].buttons


async def test_concurrent_choices_return_one_winning_receipt(delivery_env):
    env = delivery_env
    buttons = await controls(env)
    assert all(b.startswith("aqask:") and len(b) > 30 for b in buttons)
    first, second = await asyncio.gather(
        press(env, buttons[0]), press(env, buttons[1], interaction_id="press-2")
    )
    assert first["success"] and second["success"], (first, second)
    assert first["receipt"]["decision_id"] == second["receipt"]["decision_id"]
    async with env.db.immediate() as conn:
        assert len((await conn.execute(select(ask_decisions))).all()) == 1
    await settle(env)
    assert not env.sink.messages[(await binding(env))["external_root_id"]].buttons


@pytest.mark.parametrize(
    "field,value",
    [
        ("author_id", "stranger"),
        ("guild_id", "99"),
        ("channel_id", "99"),
        ("message_id", "different-root"),
    ],
)
async def test_actor_and_exact_destination_binding(delivery_env, field, value):
    env = delivery_env
    button = (await controls(env))[0]
    result = await press(env, button, **{field: value})
    assert not result["success"], result
    assert (await env.db.get_gate(env.gate["id"]))["status"] == "open"


@pytest.mark.parametrize("reason", ["expired", "revoked", "meaning", "mode"])
async def test_stale_buttons_apply_nothing_and_refresh(delivery_env, reason):
    env = delivery_env
    button = (await controls(env))[0]
    if reason == "meaning":
        await change_title(env, "Updated display")
    else:
        async with env.db.immediate() as conn:
            if reason == "mode":
                await conn.execute(
                    update(human_communication_modes).values(mode="transferring", generation=2)
                )
            else:
                values = (
                    {"created_at": 1, "expires_at": 2} if reason == "expired" else {"revoked_at": 1}
                )
                await conn.execute(update(ask_action_tokens).values(**values))
    result = await press(env, button)
    assert result["error_code"] == "ask.stale_button", result
    assert (await env.db.get_gate(env.gate["id"]))["status"] == "open"
    if reason != "mode":
        await settle(env)
        assert button not in env.sink.messages[(await binding(env))["external_root_id"]].buttons


async def test_button_gateway_authority_is_not_forgeable_from_local_command(delivery_env):
    env = delivery_env
    button = (await controls(env))[0]
    result = await env.handler._cmd_ask_button(
        {
            "interaction_id": "forged",
            "custom_id": button,
            "guild_id": "1",
            "channel_id": "2",
            "message_id": (await binding(env))["external_root_id"],
            "author_id": "3",
        }
    )
    assert result["error_code"] == "ask.gateway_only"


async def test_pause_does_not_disable_valid_decision_or_resume_project(delivery_env):
    from src.database.tables import projects

    env = delivery_env
    button = (await controls(env))[0]
    async with env.db.immediate() as conn:
        await conn.execute(update(projects).values(status="PAUSED"))
    result = await press(env, button)
    assert result["success"], result
    async with env.db.immediate() as conn:
        assert await conn.scalar(select(projects.c.status)) == "PAUSED"


async def test_gateway_interaction_defers_and_returns_private_receipt(delivery_env):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from src.discord.ask_buttons import AskButtonPress

    env = delivery_env
    button = (await controls(env))[0]
    interaction = SimpleNamespace(
        data={"custom_id": button},
        id="interaction-ui",
        guild_id=1,
        channel_id=2,
        user=SimpleNamespace(id=3),
        message=SimpleNamespace(id=(await binding(env))["external_root_id"]),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    result = await AskButtonPress(env.handler).handle(interaction)
    assert result["success"], result
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
