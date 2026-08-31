"""A live playbook compiler gets only its intended authoring commands."""

from __future__ import annotations

import time

import pytest

from src.api.auth import RequestScope
from src.api.scope import check_request_scope
from src.database import Database
from src.models import Agent, AgentProfile, AgentState, Project, SessionRecord, Task, TaskStatus

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def compiler(tmp_path):
    db = Database(str(tmp_path / "compiler-scope.db"))
    await db.initialize()
    await db.create_project(Project(id="p", name="Project"))
    for profile_id in ("playbook-compiler", "worker"):
        await db.create_profile(AgentProfile(id=profile_id, name=profile_id))
    for agent_id, profile_id in (("compiler", "playbook-compiler"), ("worker", "worker")):
        task_id = agent_id + "-task"
        await db.create_agent(Agent(id=agent_id, name=agent_id, profile_id="worker"))
        await db.create_task(
            Task(
                id=task_id,
                project_id="p",
                title=task_id,
                description="Compile one playbook",
                status=TaskStatus.IN_PROGRESS,
                profile_id=profile_id,
                assigned_agent_id=agent_id,
            )
        )
        await db.update_agent(agent_id, state=AgentState.BUSY, current_task_id=task_id)
        await db.create_session(
            SessionRecord(
                id="s-" + agent_id,
                task_id=task_id,
                project_id="p",
                agent_id=agent_id,
                profile_id=profile_id,
                harness="claude",
                provider="fake",
                name="s-" + agent_id,
                lifecycle="task",
                state="running",
                desired_state="running",
                work_dir=str(tmp_path),
                epoch="test",
                instance_token="instance-" + agent_id,
                started_at=time.time(),
                last_claim_epoch=0,
            )
        )
    yield db
    await db.close()


def scope(agent_id="compiler"):
    return RequestScope(
        kind="session",
        session_id="s-" + agent_id,
        task_id=agent_id + "-task",
        project_id="p",
    )


@pytest.mark.parametrize("command", ["playbook_validate", "playbook_install"])
async def test_live_compiler_can_validate_and_install_its_artifact(compiler, command):
    args = {}
    assert await check_request_scope(command, args, scope(), db=compiler) is None
    assert args == {
        "task_id": "compiler-task",
        "project_id": "p",
        "session_id": "s-compiler",
    }


@pytest.mark.parametrize("command", ["playbook_validate", "playbook_install"])
async def test_ordinary_worker_cannot_claim_compiler_capabilities(compiler, command):
    assert (
        await check_request_scope(command, {}, scope("worker"), db=compiler)
        == "out of scope: " + command
    )


@pytest.mark.parametrize(
    "change",
    ["stopped", "wrong-session-profile", "blocked-task", "wrong-task-profile", "idle-agent"],
)
async def test_stale_or_changed_assignment_loses_compiler_capabilities(compiler, change):
    if change == "stopped":
        await compiler.update_session("s-compiler", state="stopped")
    elif change == "wrong-session-profile":
        await compiler.update_session("s-compiler", profile_id="worker")
    elif change == "blocked-task":
        await compiler.update_task("compiler-task", status=TaskStatus.BLOCKED)
    elif change == "wrong-task-profile":
        await compiler.update_task("compiler-task", profile_id="worker")
    else:
        await compiler.update_agent("compiler", state=AgentState.IDLE, current_task_id=None)
    assert (
        await check_request_scope("playbook_install", {}, scope(), db=compiler)
        == "out of scope: playbook_install"
    )


async def test_compiler_cannot_spoof_another_identity(compiler):
    args = {"task_id": "worker-task"}
    assert (
        await check_request_scope("playbook_install", args, scope(), db=compiler)
        == "out of scope: task_id mismatch"
    )
