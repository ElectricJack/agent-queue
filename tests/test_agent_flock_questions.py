"""The flock waiting badge is derived from the exact live claim, never stored as agent state."""
from unittest.mock import AsyncMock

import httpx
import pytest

from src.models import Agent, AgentState, Project, SessionRecord, Task, TaskStatus
from tests.test_agent_flock import handler as handler, _flock_api


async def pending_worker(handler, monkeypatch, **question_changes):
    await handler.db.create_project(Project(id="p", name="Project"))
    await handler.db.create_agent(Agent(
        id="a", name="Ada", profile_id="coder",
    ))
    await handler.db.create_task(Task(
        id="task", project_id="p", title="Current task", description="Work",
        status=TaskStatus.IN_PROGRESS, assigned_agent_id="a", claim_epoch=7,
    ))
    await handler.db.update_task("task", claim_epoch=7)
    await handler.db.update_agent("a", state=AgentState.BUSY, current_task_id="task")
    await handler.db.create_session(SessionRecord(
        id="s", project_id="p", profile_id="coder", harness="claude", provider="tmux",
        name="original-pane", lifecycle="task", work_dir="/tmp", epoch="e",
        instance_token="original-instance", started_at=1, agent_id="a", task_id="task",
        state="running", desired_state="running", last_claim_epoch=7,
    ))
    q = {
        "id": "q", "session_id": "s", "session_name": "original-pane",
        "instance_token": "original-instance", "task_id": "task", "project_id": "p",
        "agent_id": "a", "claim_epoch": 7, "question": "May I deploy?",
        "state": "human", "requires_human": True, "created_at": 2.0, **question_changes,
    }
    getter = AsyncMock(return_value=[q])
    monkeypatch.setattr(handler.db, "list_agent_questions", getter)
    return q, getter


async def test_flock_and_typed_response_show_waiting_without_mutating_claim(handler, monkeypatch):
    _, getter = await pending_worker(handler, monkeypatch)
    response = await handler._cmd_list_agents({})
    row = response["agents"][0]
    assert row["waiting_question"]["question"] == "May I deploy?"
    assert row["waiting_question"]["state"] == "human"
    assert row["state"] == "busy"
    assert row["current_task_id"] == "task"
    assert row["session_id"] == "s"
    assert "instance_token" not in row["waiting_question"]
    getter.assert_awaited_once()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_flock_api(handler)),
                                 base_url="http://test") as client:
        typed = await client.post("/api/agent/list", json={})
    assert typed.status_code == 200, typed.text
    assert typed.json()["agents"][0]["waiting_question"] == row["waiting_question"]
    current = await handler.db.get_task("task")
    assert current.status == TaskStatus.IN_PROGRESS
    assert current.claim_epoch == 7
    assert (await handler.db.get_agent("a")).state == AgentState.BUSY


@pytest.mark.parametrize("state", ["supervisor", "human", "answered"])
async def test_pending_states_remain_visible_for_disabled_but_live_worker(handler, monkeypatch, state):
    await pending_worker(handler, monkeypatch, state=state)
    await handler.db.update_agent("a", enabled=False)
    await handler.db.update_session("s", lifecycle="pool", claim_phase="active")
    row = await handler._cmd_get_agent({"agent_id": "a"})
    assert row["waiting_question"]["state"] == state
    assert row["enabled"] is False
    assert row["state"] == "busy"


@pytest.mark.parametrize("mismatch", [
    {"instance_token": "older-instance"},
    {"claim_epoch": 6},
    {"task_id": "old-task"},
    {"session_id": "old-session"},
    {"session_name": "old-pane"},
    {"agent_id": "other"},
    {"project_id": "other"},
    {"state": "delivered"},
    {"state": "resolved"},
    {"state": "stale"},
])
async def test_old_questions_never_label_a_new_claim(handler, monkeypatch, mismatch):
    await pending_worker(handler, monkeypatch, **mismatch)
    row = (await handler._cmd_list_agents({}))["agents"][0]
    assert row["waiting_question"] is None


@pytest.mark.parametrize("session_updates", [
    {"state": "stopped"}, {"desired_state": "stopped"}, {"lifecycle": "named"},
])
async def test_stopped_or_freeform_sessions_do_not_show_waiting_question(handler, monkeypatch, session_updates):
    await pending_worker(handler, monkeypatch)
    await handler.db.update_session("s", **session_updates)
    row = (await handler._cmd_list_agents({}))["agents"][0]
    assert row["waiting_question"] is None


async def test_closing_pool_claim_does_not_show_a_pending_question(handler, monkeypatch):
    await pending_worker(handler, monkeypatch)
    await handler.db.update_session("s", lifecycle="pool", claim_phase="closing")
    row = (await handler._cmd_list_agents({}))["agents"][0]
    assert row["waiting_question"] is None
