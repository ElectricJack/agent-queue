"""Scoped collaboration commands, contracts, grants and CLI on disposable PostgreSQL."""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from sqlalchemy import func, select, update

from src.commands import CommandHandler
from src.commands.contracts import CONTRACTS
from src.commands.message_commands import MESSAGES_DISABLED_ERROR
from src.config import AppConfig
from src.database import Database
from src.database.tables import messages, sessions, tasks
from src.models import (
    Agent,
    AgentProfile,
    AgentState,
    Project,
    SessionRecord,
    Task,
    TaskStatus,
)
from tests.db_fixtures import lease_dsn

NOW = time.time()
COMMANDS = [
    "collaboration_create",
    "collaboration_accept",
    "collaboration_get",
    "collaboration_list",
    "collaboration_close",
]
#: Held tasks with a live pool session each; ``three`` is READY and unclaimed.
HELD = {"one": "p", "two": "p", "four": "p", "foreign": "q", "foreign2": "q"}


@pytest.fixture
async def env():
    db = Database(lease_dsn("collaboration-commands"))
    await db.initialize()
    await db.create_project(Project(id="p", name="P"))
    await db.create_project(Project(id="q", name="Q"))
    for task_id, project_id in HELD.items():
        agent_id = f"agent-{task_id}"
        await db.create_agent(
            Agent(id=agent_id, name=agent_id, profile_id="worker-codex", state=AgentState.BUSY)
        )
        await db.create_task(
            Task(
                id=task_id,
                title=task_id,
                description="",
                project_id=project_id,
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id=agent_id,
            )
        )
        await db.update_agent(agent_id, current_task_id=task_id)
        await db.create_session(
            SessionRecord(
                id=f"s-{task_id}",
                project_id=project_id,
                profile_id="worker-codex",
                harness="codex",
                provider="fake",
                name=task_id,
                lifecycle="pool",
                work_dir="/tmp/collaboration",
                epoch="boot",
                instance_token=f"token-{task_id}",
                started_at=NOW,
                task_id=task_id,
                state="running",
                agent_id=agent_id,
                claim_phase="active",
                last_claim_epoch=1,
            )
        )
    await db.create_task(
        Task(id="three", title="three", description="", project_id="p", status=TaskStatus.READY)
    )
    for session_id, project_id in (("sup-p", "p"), ("sup-global", None)):
        await db.create_session(
            SessionRecord(
                id=session_id,
                project_id=project_id,
                profile_id="supervisor",
                harness="codex",
                provider="fake",
                name=f"n-{session_id}",
                lifecycle="named",
                work_dir="/tmp/supervisor",
                epoch="boot",
                instance_token=f"token-{session_id}",
                started_at=NOW,
                state="running",
            )
        )
    async with db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id != "three").values(claim_epoch=1))
    for profile_id, granted in (("worker-codex", COMMANDS), ("supervisor", COMMANDS)):
        # The worker is granted create on purpose: the command itself must refuse it.
        await db.create_profile(
            AgentProfile(
                id=profile_id,
                name=profile_id,
                aq_commands=list(granted),
                harness_tools=[],
                plugin_tools=[],
            )
        )
    config = AppConfig()
    config.messages.enabled = True
    bus = SimpleNamespace(emit=AsyncMock())
    orch = SimpleNamespace(db=db, bus=bus, plugin_registry=None)
    yield SimpleNamespace(db=db, bus=bus, commands=CommandHandler(orch, config))
    await db.close()


def worker(task_id="one"):
    return {
        "kind": "session",
        "session_id": f"s-{task_id}",
        "session_instance_token": f"token-{task_id}",
        "project_id": HELD[task_id],
    }


SUPERVISOR = {
    "kind": "session",
    "session_id": "sup-p",
    "session_instance_token": "token-sup-p",
    "project_id": "p",
    "elevated": True,
}
GLOBAL = {
    "kind": "session",
    "session_id": "sup-global",
    "session_instance_token": "token-sup-global",
    "project_id": None,
    "elevated": True,
}


async def run(env, name, args=None, scope=None):
    payload = dict(args or {})
    if scope is not None:
        payload["_scope"] = scope
    return await env.commands.execute(name, payload)


async def create(env, members=("one", "two"), key="pair", scope=SUPERVISOR, **extra):
    result = await run(
        env,
        "collaboration_create",
        {"task_ids": list(members), "idempotency_key": key, "goal": "Review the diff", **extra},
        scope,
    )
    assert result.get("success"), result
    return result["thread"]


async def accept(env, thread, task_id="one", epoch=1):
    result = await run(
        env,
        "collaboration_accept",
        {"thread_id": thread["id"], "claim_epoch": epoch},
        worker(task_id),
    )
    assert result.get("success"), result
    return result["thread"]


async def body_kind_count(env, body_kind):
    async with env.db._engine.connect() as conn:
        return await conn.scalar(
            select(func.count()).select_from(messages).where(messages.c.body_kind == body_kind)
        )


def created_events(env):
    return [c.args[1] for c in env.bus.emit.await_args_list if c.args[0] == "collaboration.created"]


def member(thread, task_id):
    return next(m for m in thread["members"] if m["task_id"] == task_id)


async def test_worker_cannot_create_even_with_the_capability(env):
    for epoch in (1, 0):  # a live or a stale claim alike
        result = await run(
            env,
            "collaboration_create",
            {"task_ids": ["one", "two"], "idempotency_key": "k", "claim_epoch": epoch},
            worker(),
        )
        assert result["error_code"] == "collaboration.out_of_scope"
    assert await env.db.list_collaboration_threads(project_id="p") == []
    assert await body_kind_count(env, "collaboration_invite") == 0


async def test_supervisor_create_invites_each_member_once_and_replays(env):
    thread = await create(env, members=("one", "two", "three"))
    assert thread["created_by_kind"] == "supervisor"
    assert thread["created_by_id"] == "supervisor-p"
    assert {m["task_id"] for m in thread["members"]} == {"one", "two", "three"}
    assert all(m["state"] == "invited" and m["needs_accept"] for m in thread["members"])
    assert 7100 < thread["remaining_seconds"] <= 7200
    assert await body_kind_count(env, "collaboration_invite") == 3
    [payload] = created_events(env)
    assert payload["thread_id"] == thread["id"] and payload["task_ids"] == ["one", "three", "two"]

    replay = await run(
        env,
        "collaboration_create",
        {"task_ids": ["three", "two", "one"], "idempotency_key": "pair", "goal": "Review the diff"},
        SUPERVISOR,
    )
    assert replay["success"] and replay["replayed"]
    assert replay["thread"]["id"] == thread["id"]
    assert await body_kind_count(env, "collaboration_invite") == 3
    assert len(created_events(env)) == 1

    conflict = await run(
        env,
        "collaboration_create",
        {"task_ids": ["one", "two"], "idempotency_key": "pair"},
        SUPERVISOR,
    )
    assert conflict["error_code"] == "collaboration.idempotency_conflict"


async def test_operator_and_global_supervisor_create_in_a_named_project(env):
    operator = await create(env, key="op", scope=None, project_id="p")
    assert (operator["created_by_kind"], operator["created_by_id"]) == ("operator", "operator")
    global_thread = await create(env, key="global", scope=GLOBAL, project_id="p")
    assert global_thread["created_by_id"] == "supervisor-global"
    missing = await run(
        env, "collaboration_create", {"task_ids": ["one", "two"], "idempotency_key": "x"}, GLOBAL
    )
    assert missing["error_code"] == "collaboration.invalid"


@pytest.mark.parametrize(
    "args",
    [
        {"task_ids": ["one"]},
        {"task_ids": ["one", "one"]},
        {"task_ids": ["one", "foreign"]},
        {"task_ids": ["one", "two"], "deadline_seconds": 7201},
        {"task_ids": ["one", "two"], "message_budget": 41},
        {"task_ids": ["one", "two"], "surprise": True},
    ],
)
async def test_create_refuses_invalid_requests(env, args):
    result = await run(env, "collaboration_create", {"idempotency_key": "bad", **args}, SUPERVISOR)
    assert result["error_code"] == "collaboration.invalid", result


async def test_create_refuses_with_the_messages_disabled_error(env):
    env.commands.config.messages.enabled = False
    result = await run(
        env, "collaboration_create", {"task_ids": ["one", "two"], "idempotency_key": "k"},
        SUPERVISOR,
    )
    assert result == {"success": False, "error": MESSAGES_DISABLED_ERROR}


async def test_accept_is_for_the_held_task_under_its_live_claim(env):
    thread = await create(env)
    for args, code in (
        ({}, "collaboration.stale_claim"),  # a pool mutation must carry its epoch
        ({"claim_epoch": 0}, "collaboration.stale_claim"),
        ({"claim_epoch": 1, "task_id": "two"}, "collaboration.out_of_scope"),
        ({"claim_epoch": 1, "session_id": "s-two"}, "collaboration.out_of_scope"),
    ):
        result = await run(
            env, "collaboration_accept", {"thread_id": thread["id"], **args}, worker()
        )
        assert result["error_code"] == code, (args, result)
    for scope in (SUPERVISOR, None):
        result = await run(env, "collaboration_accept", {"thread_id": thread["id"]}, scope)
        assert result["error_code"] == "collaboration.out_of_scope"
    stolen = dict(worker(), session_instance_token="old")
    result = await run(
        env, "collaboration_accept", {"thread_id": thread["id"], "claim_epoch": 1}, stolen
    )
    assert result["error_code"] == "collaboration.out_of_scope"

    joined = await accept(env, thread)
    assert member(joined, "one")["state"] == "accepted"
    assert member(joined, "one")["accepted_claim_epoch"] == 1
    assert not member(joined, "one")["needs_accept"]
    assert member(joined, "two")["state"] == "invited"


async def test_unknown_foreign_and_non_member_threads_are_the_same_not_found(env):
    ours = await create(env)
    theirs = await create(env, members=("foreign", "foreign2"), key="q", scope=None, project_id="q")
    answers = []
    for scope, thread_id in (
        (worker("one"), "collab-0000000000000000"),
        (worker("one"), theirs["id"]),
        (worker("four"), ours["id"]),
    ):
        for name, extra in (("collaboration_get", {}), ("collaboration_accept", {"claim_epoch": 1})):
            result = await run(env, name, {"thread_id": thread_id, **extra}, scope)
            answers.append((result["error_code"], result["error"]))
        closed = await run(
            env, "collaboration_close", {"thread_id": thread_id, "claim_epoch": 1}, scope
        )
        answers.append((closed["error_code"], closed["error"]))
    assert set(answers) == {("collaboration.not_found", "Collaboration thread not found")}
    project_supervisor = await run(env, "collaboration_get", {"thread_id": theirs["id"]}, SUPERVISOR)
    assert project_supervisor["error_code"] == "collaboration.not_found"
    # The operator and the global supervisor read any project's thread by id.
    for scope in (None, GLOBAL):
        shown = await run(env, "collaboration_get", {"thread_id": theirs["id"]}, scope)
        assert shown["thread"]["project_id"] == "q"


async def test_removed_member_loses_read_access(env):
    thread = await create(env, members=("one", "two", "four"))
    removed = await run(
        env, "collaboration_close", {"thread_id": thread["id"], "remove_task_id": "four"},
        SUPERVISOR,
    )
    assert removed["thread"]["state"] == "active"
    assert member(removed["thread"], "four")["state"] == "removed"
    result = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker("four"))
    assert result["error_code"] == "collaboration.not_found"
    assert (await run(env, "collaboration_list", {}, worker("four")))["count"] == 0


async def test_needs_accept_flips_after_claim_rollover(env):
    thread = await create(env)
    await accept(env, thread)
    shown = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker())
    assert not member(shown["thread"], "one")["needs_accept"]
    async with env.db._engine.begin() as conn:
        await conn.execute(update(tasks).where(tasks.c.id == "one").values(claim_epoch=2))
        await conn.execute(
            update(sessions).where(sessions.c.id == "s-one").values(last_claim_epoch=2)
        )
    shown = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker())
    assert member(shown["thread"], "one")["needs_accept"]
    assert f"aq collaboration accept {thread['id']}" in shown["next_step"]
    stale = await run(
        env, "collaboration_accept", {"thread_id": thread["id"], "claim_epoch": 1}, worker()
    )
    assert stale["error_code"] == "collaboration.stale_claim"
    rejoined = await accept(env, thread, epoch=2)
    assert member(rejoined, "one")["accepted_claim_epoch"] == 2
    assert not member(rejoined, "one")["needs_accept"]


async def test_capacity_hold_when_no_partner_is_running(env):
    thread = await create(env, members=("one", "three"))
    await accept(env, thread)
    held = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker())
    assert member(held["thread"], "three")["task_status"] == "READY"
    assert held["capacity_hold"] is True
    assert "Do not wait" in held["next_step"]
    # Elevated readers are not members and hold no seat.
    assert (await run(env, "collaboration_get", {"thread_id": thread["id"]}, SUPERVISOR))[
        "capacity_hold"
    ] is False
    async with env.db._engine.begin() as conn:
        await conn.execute(
            update(tasks)
            .where(tasks.c.id == "three")
            .values(status="IN_PROGRESS", assigned_agent_id="agent-four", claim_epoch=1)
        )
    running = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker())
    assert member(running["thread"], "three")["running"]
    assert running["capacity_hold"] is False
    assert f"aq message wait --thread {thread['id']}" in running["next_step"]


async def test_get_reads_the_tail_or_after_a_cursor(env):
    thread = await create(env)
    await accept(env, thread, "one")
    await accept(env, thread, "two")
    for index, sender in enumerate(("one", "two", "one")):
        await env.db.append_collaboration_message(
            thread_id=thread["id"],
            project_id="p",
            sender_task_id=sender,
            sender_claim_epoch=1,
            sender_session_id=f"s-{sender}",
            client_key=f"k{index}",
            body=f"body {index}",
            now=time.time(),
        )
    tail = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker("two"))
    assert [m["seq"] for m in tail["messages"]] == [1, 2, 3]
    assert tail["messages"][0]["body"] == "body 0"
    assert tail["next_cursor"] == 3 and tail["has_more"] is False
    assert tail["thread"]["message_count"] == 3
    page = await run(
        env, "collaboration_get", {"thread_id": thread["id"], "after_seq": 1}, worker("two")
    )
    assert [m["sender_task_id"] for m in page["messages"]] == ["two", "one"]
    empty = await run(
        env, "collaboration_get", {"thread_id": thread["id"], "after_seq": 3}, worker("two")
    )
    assert empty["messages"] == [] and empty["next_cursor"] == 3


async def test_list_pins_a_worker_to_its_held_task(env):
    await create(env, key="a")
    await create(env, members=("two", "four"), key="b")
    mine = await run(env, "collaboration_list", {}, worker("one"))
    assert mine["count"] == 1
    assert {m["task_id"] for m in mine["threads"][0]["members"]} == {"one", "two"}
    nominated = await run(env, "collaboration_list", {"task_id": "four"}, worker("one"))
    assert nominated["error_code"] == "collaboration.out_of_scope"
    assert (await run(env, "collaboration_list", {}, SUPERVISOR))["count"] == 2
    # The operator may name only a task; its project is derived.
    by_task = await run(env, "collaboration_list", {"task_id": "four"})
    assert by_task["count"] == 1
    assert (await run(env, "collaboration_list", {"state": "closed"}, SUPERVISOR))["count"] == 0
    assert (await run(env, "collaboration_list", {}))["error_code"] == "collaboration.invalid"


async def test_member_close_never_changes_member_tasks(env):
    thread = await create(env)
    before = {t: (await env.db.get_task(t)).status for t in ("one", "two")}
    result = await run(
        env, "collaboration_close", {"thread_id": thread["id"], "claim_epoch": 1, "note": "done"},
        worker(),
    )
    assert result["success"], result
    assert result["thread"]["state"] == "closed"
    assert result["thread"]["final_result"]["note"] == "done"
    assert result["thread"]["remaining_seconds"] == 0
    assert {t: (await env.db.get_task(t)).status for t in ("one", "two")} == before
    again = await run(
        env, "collaboration_close", {"thread_id": thread["id"], "claim_epoch": 1}, worker("two")
    )
    assert again["thread"]["version"] == result["thread"]["version"]
    assert await body_kind_count(env, "collaboration_closed") == 2
    shown = await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker())
    assert shown["capacity_hold"] is False and "has ended (closed)" in shown["next_step"]


async def test_remove_and_all_active_are_elevated_only(env):
    pair = await create(env)
    for args in ({"thread_id": pair["id"], "remove_task_id": "two"}, {"all_active": True}):
        result = await run(env, "collaboration_close", {**args, "claim_epoch": 1}, worker())
        assert result["error_code"] == "collaboration.out_of_scope", args
    removed = await run(
        env, "collaboration_close", {"thread_id": pair["id"], "remove_task_id": "two"},
        SUPERVISOR,
    )
    assert removed["thread"]["state"] == "closed"
    assert removed["thread"]["close_reason"] == "members_below_two"
    assert (await env.db.get_task("two")).status == TaskStatus.IN_PROGRESS

    ids = {(await create(env, key=f"k{i}"))["id"] for i in range(3)}
    other = await create(env, members=("foreign", "foreign2"), key="q", scope=None, project_id="q")
    closed = await run(env, "collaboration_close", {"all_active": True, "note": "rollback"},
                       SUPERVISOR)
    assert closed["success"], closed
    assert closed["closed_count"] == 3 and set(closed["thread_ids"]) == ids
    assert (await run(env, "collaboration_list", {"state": "active"}, SUPERVISOR))["count"] == 0
    still = await run(env, "collaboration_get", {"thread_id": other["id"]})
    assert still["thread"]["state"] == "active"
    again = await run(env, "collaboration_close", {"all_active": True}, SUPERVISOR)
    assert again["closed_count"] == 0
    assert (await env.db.get_task("one")).status == TaskStatus.IN_PROGRESS


async def test_no_command_writes_session_activity_or_pane_input(env, monkeypatch):
    from src.sessions.provider import SessionProvider

    activity = AsyncMock()
    pane_input = AsyncMock()
    provider_input = AsyncMock()
    monkeypatch.setattr(env.db, "touch_session_activity", activity)
    monkeypatch.setattr(env.commands, "_cmd_session_input", pane_input)
    monkeypatch.setattr(SessionProvider, "send_input", provider_input)
    thread = await create(env)
    await accept(env, thread, "one")
    await accept(env, thread, "two")
    await run(env, "collaboration_get", {"thread_id": thread["id"]}, worker())
    await run(env, "collaboration_list", {}, worker())
    await run(env, "collaboration_close", {"thread_id": thread["id"], "claim_epoch": 1}, worker())
    activity.assert_not_called()
    pane_input.assert_not_called()
    provider_input.assert_not_called()
    assert (await env.db.get_session("s-one")).last_activity is None


def test_contracts_scope_and_grants():
    from src.api.scope import AGENT_COMMAND_SET
    from src.profiles.parser import parse_profile

    agent_surface = set(COMMANDS) - {"collaboration_create"}
    assert agent_surface <= AGENT_COMMAND_SET
    assert "collaboration_create" not in AGENT_COMMAND_SET
    for name in COMMANDS:
        execution = CONTRACTS.require(name).contract.execution
        assert execution.capability == name and execution.retry_safe
    assert CONTRACTS.require("collaboration_create").contract.execution.idempotency.key_field == (
        "idempotency_key"
    )
    defaults = Path("src/profiles/defaults")
    for template in ("worker-codex", "worker-claude"):
        granted = set(
            parse_profile((defaults / template / "profile.md").read_text()).capabilities[
                "aq_commands"
            ]
        )
        assert agent_surface <= granted and "collaboration_create" not in granted
    supervisor = parse_profile((defaults / "supervisor" / "profile.md").read_text())
    assert set(COMMANDS) <= set(supervisor.capabilities["aq_commands"])


def test_collaboration_cli_builds_params_and_renders(monkeypatch):
    from src.cli import collaboration
    from src.cli.app import cli

    calls = []
    thread = {
        "id": "collab-1",
        "state": "active",
        "goal": "Review",
        "remaining_seconds": 125,
        "message_count": 1,
        "message_budget": 40,
        "last_seq": 1,
        "members": [
            {"task_id": "one", "task_status": "IN_PROGRESS", "running": True,
             "state": "accepted", "needs_accept": False},
            {"task_id": "two", "task_status": "READY", "running": False,
             "state": "invited", "needs_accept": True},
        ],
    }

    @asynccontextmanager
    async def client(*args):
        async def execute(name, params):
            calls.append((name, params))
            return {
                "success": True,
                "thread": thread,
                "messages": [
                    {"seq": 1, "sender_task_id": "one", "body": "<b>hi</b>", "created_at": NOW}
                ],
                "capacity_hold": True,
                "next_step": "Do not wait.",
            }

        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(collaboration, "_get_client", client)
    monkeypatch.setattr(
        collaboration, "resolve_claim_epoch", lambda explicit: 7 if explicit is None else explicit
    )
    runner = CliRunner()
    created = runner.invoke(cli, [
        "collaboration", "create", "--project", "p", "--task", "one", "--task", "two",
        "--deadline-minutes", "30", "--budget", "10", "--idempotency-key", "k", "--json",
    ])
    assert created.exit_code == 0, created.output
    assert json.loads(created.output)["data"]["thread"]["id"] == "collab-1"
    assert calls[-1] == ("collaboration_create", {
        "project_id": "p", "task_ids": ["one", "two"], "deadline_seconds": 1800,
        "message_budget": 10, "idempotency_key": "k",
    })
    assert runner.invoke(cli, ["collaboration", "accept", "collab-1"]).exit_code == 0
    assert calls[-1] == ("collaboration_accept", {"thread_id": "collab-1", "claim_epoch": 7})
    shown = runner.invoke(cli, ["collaboration", "show", "collab-1", "--after", "2"])
    assert shown.exit_code == 0, shown.output
    assert calls[-1] == ("collaboration_get", {"thread_id": "collab-1", "after_seq": 2})
    for text in ("2m05s left", "budget 1/40", "two  READY  not running  needs accept",
                 "#1  one", "<b>hi</b>", "capacity hold", "Do not wait."):
        assert text in shown.output, text
    assert runner.invoke(cli, ["collaboration", "list", "--state", "active"]).exit_code == 0
    assert calls[-1] == ("collaboration_list", {"state": "active", "limit": 20})
    closed = runner.invoke(
        cli, ["collaboration", "close", "--all-active", "--project", "p", "--claim-epoch", "3"]
    )
    assert closed.exit_code == 0, closed.output
    assert calls[-1] == (
        "collaboration_close", {"all_active": True, "project_id": "p", "claim_epoch": 3}
    )
    both = runner.invoke(cli, ["collaboration", "close", "collab-1", "--all-active"])
    assert both.exit_code != 0 and calls[-1][0] == "collaboration_close"
    assert len([c for c in calls if c[0] == "collaboration_close"]) == 1
