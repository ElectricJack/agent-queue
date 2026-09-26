"""Scoped wait command, contract, grant and CLI behavior."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import update

from src.agent_waits import AgentWaitReconciler
from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.commands.principal import ExecutionPrincipal, PrincipalKind, principal_context
from src.config import AppConfig
from src.database.tables import tasks
from src.models import AgentProfile
from src.profiles.capabilities import DENY_ALL
from tests.test_agent_wait_queries import NOW, env as _wait_env

env = _wait_env


@pytest.fixture
async def commands(env):
    await env.db.create_profile(
        AgentProfile(
            id="worker-codex",
            name="Worker",
            aq_commands=["wait_register", "wait_get", "wait_list", "wait_cancel"],
            harness_tools=[],
            plugin_tools=[],
        )
    )
    config = AppConfig()
    orch = SimpleNamespace(db=env.db, bus=SimpleNamespace(emit=AsyncMock()), plugin_registry=None)
    return CommandHandler(orch, config)


def scope(**extra):
    return dict(
        kind="session", session_id="s", session_instance_token="token", project_id="p", **extra
    )


async def execute(commands, name, args=None, **extra_scope):
    return await commands.execute(name, {**(args or {}), "_scope": scope(**extra_scope)})


async def test_register_show_list_cancel_owner_is_derived(commands, env):
    # A task ref names the producer, not the owner in server scope.
    result = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", timeout=100, idempotency_key="key", claim_epoch=1),
    )
    assert result["success"], result
    row = result["wait"]
    assert row["owner_id"] == "owner" and row["session_id"] == "s"
    assert "End this turn" in result["next_step"]
    shown = await execute(commands, "wait_get", {"wait_id": row["id"]})
    assert shown["wait"]["id"] == row["id"]
    listed = await execute(commands, "wait_list")
    assert listed["count"] == 1
    cancelled = await execute(commands, "wait_cancel", {"wait_id": row["id"], "claim_epoch": 1})
    assert cancelled["wait"]["state"] == "cancelled"
    again = await execute(commands, "wait_cancel", {"wait_id": row["id"], "claim_epoch": 1})
    assert again["wait"]["version"] == cancelled["wait"]["version"]


@pytest.mark.parametrize(
    "fields",
    [
        {"task_id": "source"},
        {"session_id": "someone-else"},
        {"project_id": "q"},
        {"owner_id": "source"},
        {"claim_epoch": 0},
        {"claim_epoch": None},
        {"timeout": 86401},
        {"timeout": 0},
        {"timeout": float("nan")},
        {"due_at": NOW, "kind": "task"},
    ],
)
async def test_register_rejects_forged_owner_stale_epoch_and_unbounded_wait(commands, fields):
    result = await execute(
        commands,
        "wait_register",
        {
            "kind": "task",
            "ref": "source",
            "idempotency_key": "invalid",
            "claim_epoch": 1,
            **fields,
        },
    )
    assert result.get("error"), result


async def test_foreign_target_and_missing_job_adapter(commands):
    for kind, ref, code in (
        ("task", "foreign", "out_of_scope"),
        ("job", "job-id", "wait.adapter_unavailable"),
    ):
        result = await execute(
            commands,
            "wait_register",
            dict(kind=kind, ref=ref, idempotency_key="key", claim_epoch=1),
        )
        assert result["error_code"] == code


async def test_history_across_epochs_remains_readable_but_not_cancellable(commands, env):
    registered = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", idempotency_key="key", claim_epoch=1),
    )
    wait_id = registered["wait"]["id"]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "owner").values(claim_epoch=2))
    shown = await execute(commands, "wait_get", {"wait_id": wait_id})
    assert shown["success"]
    cancelled = await execute(commands, "wait_cancel", {"wait_id": wait_id, "claim_epoch": 1})
    assert cancelled["error_code"] == "stale_claim"


async def test_read_cannot_cross_owners(commands, env):
    registered = await execute(
        commands,
        "wait_register",
        dict(kind="task", ref="source", idempotency_key="key", claim_epoch=1),
    )
    wait_id = registered["wait"]["id"]
    async with env.db._engine.begin() as conn:
        from src.database.tables import agent_waits

        await conn.execute(
            update(agent_waits).where(agent_waits.c.id == wait_id).values(owner_id="other")
        )
    result = await execute(commands, "wait_get", {"wait_id": wait_id})
    assert result["error_code"] == "out_of_scope"
    assert (await execute(commands, "wait_list"))["count"] == 0


async def test_reconciler_is_service_only_and_calls_command_boundary(commands):
    rejected = await execute(commands, "reconcile_agent_waits")
    assert rejected.get("error")
    result = await AgentWaitReconciler(commands).tick(now=NOW)
    assert result["success"] and result["scanned"] == 0


async def test_supplied_principal_cannot_replace_owner_instance(commands):
    principal = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="s",
        session_instance_token="old",
        project_id="p",
        provenance=("profile-not-found",),
    )
    with principal_context(principal):
        result = await commands.execute(
            "wait_register", dict(kind="task", ref="source", claim_epoch=1, idempotency_key="key")
        )
    assert result.get("error")


def test_four_contracts_are_granted_and_typed():
    from src.api.scope import AGENT_COMMAND_SET
    from src.profiles.parser import parse_profile
    from pathlib import Path

    names = {"wait_register", "wait_get", "wait_list", "wait_cancel"}
    assert names <= AGENT_COMMAND_SET
    for name in names:
        contract = CONTRACTS.require(name).contract
        assert contract.execution.capability == name
        assert contract.execution.retry_safe
    for template in ("worker-codex", "worker-claude"):
        profile = parse_profile(Path(f"src/profiles/defaults/{template}/profile.md").read_text())
        assert names <= set(profile.capabilities["aq_commands"])
    assert (
        CONTRACTS.require("wait_register").contract.execution.idempotency.key_field
        == "idempotency_key"
    )


def test_wait_cli_reads_claim_epoch_and_emits_versioned_json(monkeypatch):
    from src.cli.app import cli
    from src.cli import waits

    calls = []

    @asynccontextmanager
    async def client(*args):
        async def execute(name, params):
            calls.append((name, params))
            return {"success": True, "wait": {"id": "w", "state": "active"}}

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(waits, "_get_client", client)
    monkeypatch.setattr(
        waits, "resolve_claim_epoch", lambda explicit: explicit if explicit is not None else 7
    )
    result = CliRunner().invoke(
        cli,
        ["wait", "register", "--kind", "task", "--ref", "t", "--idempotency-key", "k", "--json"],
    )
    assert result.exit_code == 0, result.output
    import json

    assert json.loads(result.output)["data"]["wait"]["id"] == "w"
    assert calls[0] == (
        "wait_register",
        {"kind": "task", "ref": "t", "idempotency_key": "k", "claim_epoch": 7},
    )
    shown = CliRunner().invoke(cli, ["wait", "show", "w", "--json"])
    assert shown.exit_code == 0
    assert calls[-1] == ("wait_get", {"wait_id": "w"})
    cancelled = CliRunner().invoke(cli, ["wait", "cancel", "w", "--claim-epoch", "8", "--json"])
    assert cancelled.exit_code == 0
    assert calls[-1] == ("wait_cancel", {"wait_id": "w", "claim_epoch": 8})


async def test_global_supervisor_subscription_does_not_block_and_pointer_needs_no_project(
    commands, env
):
    from src.models import SessionRecord

    await env.db.create_session(
        SessionRecord(
            id="global-row",
            project_id=None,
            profile_id="supervisor",
            harness="codex",
            provider="fake",
            name="n-supervisor--global",
            lifecycle="named",
            work_dir="/tmp/global",
            epoch="boot",
            instance_token="global-token",
            started_at=NOW,
            state="running",
        )
    )
    global_scope = dict(
        kind="session",
        session_id="global-row",
        project_id=None,
        session_instance_token="global-token",
        elevated=True,
    )
    registered = await commands.execute(
        "wait_register",
        {
            "kind": "task",
            "ref": "source",
            "project_id": "p",
            "idempotency_key": "subscription",
            "_scope": global_scope,
        },
    )
    assert registered["success"], registered
    row = registered["wait"]
    assert row["owner_kind"] == "supervisor" and row["owner_id"] == "supervisor-global"
    assert "Continue working" in registered["next_step"]
    shown = await commands.execute("wait_get", {"wait_id": row["id"], "_scope": global_scope})
    assert shown["success"], shown
    cancelled = await commands.execute(
        "wait_cancel", {"wait_id": row["id"], "_scope": global_scope}
    )
    assert cancelled["success"] and cancelled["wait"]["state"] == "cancelled"
    result = await env.db.get_message(cancelled["wait"]["result_message_id"])
    assert result.to_kind == "session" and result.to_id == "supervisor-global"
