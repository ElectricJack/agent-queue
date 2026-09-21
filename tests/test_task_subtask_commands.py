"""The four subtask commands: durable checklist rows fenced like task comments."""

from __future__ import annotations

import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.api.auth import RequestScope
from src.commands.handler import CommandHandler
from src.config import AppConfig, DatabaseConfig
from src.database import Database
from src.models import Agent, AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.orchestrator import Orchestrator
from tests.db_fixtures import lease_dsn

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def env(tmp_path):
    db = Database(lease_dsn("subtasks.db"))
    await db.initialize()
    for pid in ("p", "other"):
        await db.create_project(Project(id=pid, name=pid))
    await db.create_profile(AgentProfile(id="worker", name="Worker", needs_workspace=False))
    await db.create_agent(Agent(id="agent", name="Worker", profile_id="worker"))
    for tid, pid in (("t", "p"), ("peer", "p"), ("foreign", "other")):
        await db.create_task(
            Task(
                id=tid,
                project_id=pid,
                title=tid,
                description="d",
                status=TaskStatus.IN_PROGRESS,
                assigned_agent_id="agent" if tid == "t" else None,
            )
        )
    await db.update_task("t", claim_epoch=7)
    await db.create_session(
        SessionRecord(
            id="session",
            task_id="t",
            project_id="p",
            agent_id="agent",
            profile_id="worker",
            harness="codex",
            provider="fake",
            name="worker-session",
            lifecycle="pool",
            state="running",
            work_dir=str(tmp_path),
            epoch="test",
            instance_token="instance",
            started_at=time.time(),
        )
    )
    config = AppConfig(
        database=DatabaseConfig(url=lease_dsn("subtasks.db")),
        data_dir=str(tmp_path / "data"),
        workspace_dir=str(tmp_path / "ws"),
    )
    orch = Orchestrator(config)
    orch.db = db
    orch.bus.emit = AsyncMock()
    handler = CommandHandler(orch, config)
    scope = RequestScope(kind="session", session_id="session", project_id="p")
    yield SimpleNamespace(db=db, handler=handler, scope=scope, orch=orch)
    await db.close()


async def run(env, command, args=None, *, scope=None):
    args = dict(args or {})
    if scope is not None:
        args["_scope"] = asdict(scope)
    return await env.handler.execute(command, args)


def subtask_events(env):
    return [
        payload
        for name, payload in (call.args for call in env.orch.bus.emit.await_args_list)
        if name == "task.subtasks_updated"
    ]


async def test_add_and_list_round_trip(env):
    added = await run(
        env,
        "task_subtask_add",
        {"task_id": "t", "subtasks": [{"title": "Write tests"}, {"title": "Ship", "context": "PR"}]},
    )
    assert added["success"] is True
    assert added["task_id"] == "t"
    assert [s["title"] for s in added["subtasks"]] == ["Write tests", "Ship"]
    assert added["subtasks"][1]["context"] == "PR"

    listed = await run(env, "task_subtasks", {"task_id": "t"})
    assert listed["success"] is True
    assert listed["total"] == 2
    assert listed["settled"] == 0
    assert [s["title"] for s in listed["subtasks"]] == ["Write tests", "Ship"]
    assert "context" not in listed["subtasks"][0]


async def test_omitted_task_id_resolves_to_held_task(env):
    added = await run(
        env,
        "task_subtask_add",
        {"subtasks": [{"title": "Held task subtask"}], "claim_epoch": 7},
        scope=env.scope,
    )
    assert added["success"] is True, added
    assert added["task_id"] == "t"

    listed = await run(env, "task_subtasks", {}, scope=env.scope)
    assert listed["task_id"] == "t"
    assert listed["total"] == 1


async def test_write_is_refused_off_task_but_read_is_allowed_in_project(env):
    write = await run(
        env,
        "task_subtask_add",
        {"task_id": "peer", "subtasks": [{"title": "Not mine"}], "claim_epoch": 7},
        scope=env.scope,
    )
    assert "error" in write

    read = await run(env, "task_subtasks", {"task_id": "peer"}, scope=env.scope)
    assert read["success"] is True
    assert read["subtasks"] == []

    foreign_read = await run(env, "task_subtasks", {"task_id": "foreign"}, scope=env.scope)
    assert "error" in foreign_read


async def test_stale_claim_epoch_is_refused(env):
    result = await run(
        env,
        "task_subtask_add",
        {"task_id": "t", "subtasks": [{"title": "x"}], "claim_epoch": 6},
        scope=env.scope,
    )
    assert "error" in result
    assert (await run(env, "task_subtasks", {"task_id": "t"}))["total"] == 0


async def test_update_emits_exactly_one_event_without_title(env):
    added = await run(
        env, "task_subtask_add", {"task_id": "t", "subtasks": [{"title": "secret title"}]}
    )
    env.orch.bus.emit.reset_mock()
    ordinal = added["subtasks"][0]["ordinal"]
    updated = await run(
        env, "task_subtask_update", {"task_id": "t", "ordinal": ordinal, "status": "done"}
    )
    assert updated["success"] is True
    assert updated["subtask"]["status"] == "done"
    assert updated["total"] == 1
    assert updated["settled"] == 1

    events = subtask_events(env)
    assert len(events) == 1
    payload = events[0]
    assert payload == {"task_id": "t", "project_id": "p", "total": 1, "settled": 1}
    assert "secret title" not in str(payload)


async def test_unknown_ordinal_is_not_found(env):
    get_result = await run(env, "task_subtask_get", {"task_id": "t", "ordinal": 99})
    assert get_result == {
        "success": False,
        "code": "subtasks.not_found",
        "error": "No subtask #99 on task 't'",
    }
    update_result = await run(
        env, "task_subtask_update", {"task_id": "t", "ordinal": 99, "status": "done"}
    )
    assert update_result["success"] is False
    assert update_result["code"] == "subtasks.not_found"


async def test_bad_status_is_invalid_status(env):
    added = await run(env, "task_subtask_add", {"task_id": "t", "subtasks": [{"title": "x"}]})
    ordinal = added["subtasks"][0]["ordinal"]
    result = await run(
        env, "task_subtask_update", {"task_id": "t", "ordinal": ordinal, "status": "bogus"}
    )
    assert result["success"] is False
    assert result["code"] == "subtasks.invalid_status"


async def test_add_over_per_call_limit_is_rejected(env):
    items = [{"title": f"t{i}"} for i in range(51)]
    result = await run(env, "task_subtask_add", {"task_id": "t", "subtasks": items})
    assert result["success"] is False
    assert (await run(env, "task_subtasks", {"task_id": "t"}))["total"] == 0


async def test_add_over_per_task_limit_returns_subtasks_limit(env):
    from src.database.queries.task_subtask_queries import MAX_SUBTASKS_PER_TASK

    remaining = MAX_SUBTASKS_PER_TASK
    while remaining > 0:
        batch = min(remaining, 50)
        result = await run(
            env,
            "task_subtask_add",
            {"task_id": "t", "subtasks": [{"title": "x"} for _ in range(batch)]},
        )
        assert result["success"] is True, result
        remaining -= batch

    overflow = await run(env, "task_subtask_add", {"task_id": "t", "subtasks": [{"title": "one too many"}]})
    assert overflow == {
        "success": False,
        "code": "subtasks.limit",
        "error": (
            f"adding 1 subtask(s) would exceed the {MAX_SUBTASKS_PER_TASK} per-task limit"
        ),
    }


async def test_task_get_and_update_are_scope_fenced(env):
    added = await run(env, "task_subtask_add", {"task_id": "peer", "subtasks": [{"title": "x"}]})
    ordinal = added["subtasks"][0]["ordinal"]
    read = await run(env, "task_subtask_get", {"task_id": "peer", "ordinal": ordinal}, scope=env.scope)
    assert read["success"] is True
    write = await run(
        env,
        "task_subtask_update",
        {"task_id": "peer", "ordinal": ordinal, "status": "done"},
        scope=env.scope,
    )
    assert "error" in write
