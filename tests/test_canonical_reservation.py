"""Recovery and diagnosis of missing canonical train branch owners."""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select

from src.commands.detached import DetachedOperations
from src.commands.integration_commands import IntegrationCommandsMixin
from src.commands.principal import (
    ExecutionPrincipal,
    PrincipalKind,
    principal_context,
)
from src.commands.task_commands import TaskCommandsMixin
from src.database.tables import (
    integration_branch_owners,
    task_branch_origins,
    task_integration_checkpoints,
)
from src.integration.canonical_reservation import reserve_canonical_task_branch
from src.models import Agent, Project, RepoConfig, RepoSourceType, SessionRecord, Task, TaskStatus
from src.profiles.capabilities import DENY_ALL


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("canonical-reservation.db")
    await database.create_project(Project(id="p", name="Project"))
    await database.create_repo(
        RepoConfig(
            id="repo",
            project_id="p",
            source_type=RepoSourceType.CLONE,
        )
    )
    await database.update_project(
        "p", hierarchical_integration_mode="train", integration_repository_id="repo"
    )
    await database.create_task(
        Task(
            id="producer",
            project_id="p",
            repo_id="repo",
            branch_name="aq/producer",
            title="Producer",
            description="",
            status=TaskStatus.BLOCKED,
        )
    )
    async with database.immediate() as conn:
        await conn.execute(
            insert(task_integration_checkpoints).values(
                task_id="producer",
                repository_id="repo",
                branch="aq/producer",
                updated_at=time.time(),
            )
        )
        await conn.execute(
            insert(task_branch_origins).values(
                id="origin",
                task_id="producer",
                repository_id="repo",
                branch_name="aq/producer",
                base_sha="a" * 40,
                creation_generation=0,
                reserved=True,
                materialized=True,
                created_at=time.time(),
                materialized_at=time.time(),
            )
        )
    return database


async def _owner(db):
    async with db._engine.connect() as conn:
        return (await conn.execute(select(integration_branch_owners))).mappings().one_or_none()


async def test_restart_restores_missing_canonical_reservation_before_ready(db):
    command = SimpleNamespace(db=db)

    result = await TaskCommandsMixin._cmd_restart_task(command, {"task_id": "producer"})

    assert result["previous_status"] == "BLOCKED"
    assert (await db.get_task("producer")).status is TaskStatus.READY
    owner = await _owner(db)
    assert (owner["owner_id"], owner["owner_role"], owner["handoff_state"]) == (
        "producer",
        "worker",
        "reserved",
    )
    assert (await reserve_canonical_task_branch(db, "producer"))["outcome"] == "already_reserved"


@pytest.mark.parametrize("bound", ["elsewhere", "working_here", "session_here", "unbound"])
async def test_restart_scopes_reused_agent_assignment_to_the_producer(db, bound):
    await db.create_agent(Agent(id="reused", name="Reused", profile_id="worker"))
    await db.create_task(Task(
        id="next", project_id="p", title="Next", description="", status=TaskStatus.IN_PROGRESS,
        assigned_agent_id="reused",
    ))
    await db.update_task("producer", assigned_agent_id="reused")
    await db.update_agent(
        "reused", current_task_id="producer" if bound == "working_here" else (
            None if bound == "unbound" else "next"
        ),
    )
    await db.create_session(SessionRecord(
        id="stopped", name="stopped", task_id="producer", project_id="p", agent_id="reused",
        profile_id="worker", harness="fake", provider="fake", lifecycle="pool",
        state="stopped", desired_state="stopped", epoch="test", instance_token="stopped",
        work_dir="/never-used", started_at=50,
    ))
    async with db.immediate() as conn:
        await conn.execute(insert(integration_branch_owners).values(
            id="released", repository_id="repo", ref="aq/producer", owner_id="producer",
            owner_role="worker", handoff_state="released", fence_token=8,
            created_at=50, updated_at=100,
        ))
    for sid, task_id in (("live", "producer" if bound == "session_here" else "next"),
                         ("idle", None)):
        await db.create_session(SessionRecord(
            id=sid, name=sid, task_id=task_id, project_id="p", agent_id="reused",
            profile_id="worker", harness="fake", provider="fake", lifecycle="pool",
            state="running" if task_id else "sleeping",
            desired_state="running" if task_id else "sleeping", epoch="test",
            instance_token=sid, work_dir="/never-used", started_at=100,
        ))
    await db.set_task_meta("producer", "claimed_by_session", "live")
    await db.set_task_meta("producer", "supervisor_recovery_incident", {
        "id": "held", "decision": "hold", "decided_at": 200,
    })
    other_task = asdict(await db.get_task("next"))
    other_agent = asdict(await db.get_agent("reused"))
    other_sessions = [asdict(await db.get_session(sid)) for sid in ("live", "idle")]

    result = await TaskCommandsMixin._cmd_restart_task(SimpleNamespace(db=db), {
        "task_id": "producer", "override_hold": True,
        "reason": "Restart the released producer after pool agent reuse",
    })

    assert asdict(await db.get_task("next")) == other_task
    assert asdict(await db.get_agent("reused")) == other_agent
    assert [asdict(await db.get_session(sid)) for sid in ("live", "idle")] == other_sessions
    task = await db.get_task("producer")
    if bound == "elsewhere":
        assert result["success"], result
        assert task.status is TaskStatus.READY and task.assigned_agent_id is None
        assert (await _owner(db))["owner_id"] == "producer"
        assert (await _owner(db))["fence_token"] == 9
    else:
        assert "writer or workspace" in result["error"], result
        assert task.status is TaskStatus.BLOCKED and task.assigned_agent_id == "reused"
        assert (await db.get_task_meta("producer", "supervisor_recovery_incident"))["decision"] == "hold"
        assert (await _owner(db))["handoff_state"] == "released"
        assert (await _owner(db))["fence_token"] == 8


async def test_restart_refuses_a_competing_owner_and_keeps_task_blocked(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="other",
                repository_id="repo",
                ref="aq/producer",
                owner_id="other-task",
                owner_role="worker",
                fence_token=3,
                handoff_state="reserved",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )

    result = await TaskCommandsMixin._cmd_restart_task(
        SimpleNamespace(db=db), {"task_id": "producer"}
    )

    assert "another or unresolved owner" in result["error"]
    assert (await db.get_task("producer")).status is TaskStatus.BLOCKED
    assert (await _owner(db))["owner_id"] == "other-task"


async def test_released_row_is_reacquired_with_a_new_fence(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="released",
                repository_id="repo",
                ref="aq/producer",
                owner_id="old-task",
                owner_role="worker",
                fence_token=8,
                handoff_state="released",
                created_at=time.time(),
                updated_at=time.time(),
            )
        )

    result = await reserve_canonical_task_branch(db, "producer")

    assert result["outcome"] == "acquired"
    assert (await _owner(db))["fence_token"] == 9


async def test_reservation_refuses_another_ready_task_on_the_branch(db):
    await db.create_task(
        Task(
            id="verifier",
            project_id="p",
            repo_id="repo",
            branch_name="aq/producer",
            title="Verifier",
            description="",
            status=TaskStatus.READY,
        )
    )

    result = await reserve_canonical_task_branch(db, "producer")

    assert result["outcome"] == "not_eligible"
    assert result["reason"] == "another task uses the canonical branch"
    assert await _owner(db) is None


async def test_reserve_owner_control_is_supervisor_scoped(db):
    command = IntegrationCommandsMixin()
    command.db = db
    worker = ExecutionPrincipal(
        kind=PrincipalKind.SESSION,
        policy=DENY_ALL,
        session_id="worker",
        project_id="p",
        elevated=False,
    )
    with principal_context(worker):
        denied = await command._cmd_integration_reserve_owner({"task_id": "producer"})
    assert denied["outcome"] == "unauthorized"
    assert await _owner(db) is None

    result = await command._cmd_integration_reserve_owner({"task_id": "producer"})
    assert result["outcome"] == "acquired"
    assert (await _owner(db))["owner_id"] == "producer"


async def test_reserve_owner_refusal_names_its_reason(db):
    async with db.immediate() as conn:
        await conn.execute(
            insert(integration_branch_owners).values(
                id="other", repository_id="repo", ref="aq/producer", owner_id="other-task",
                owner_role="worker", fence_token=3, handoff_state="reserved",
                created_at=time.time(), updated_at=time.time(),
            )
        )
    command = IntegrationCommandsMixin()
    command.db = db

    result = await command._cmd_integration_reserve_owner({"task_id": "producer"})

    assert (result["success"], result["outcome"]) == (False, "not_eligible")
    assert result["error"] == (await reserve_canonical_task_branch(db, "producer"))["reason"]


async def test_slow_reserve_owner_answers_with_a_poll_handle_and_joins_the_run(db, monkeypatch):
    import src.integration.canonical_reservation as reservation

    gate, calls = asyncio.Event(), []

    async def slow(database, task_id):
        calls.append(task_id)
        await gate.wait()
        return await reserve_canonical_task_branch(database, task_id)

    monkeypatch.setattr(reservation, "reserve_canonical_task_branch", slow)
    command = IntegrationCommandsMixin()
    command.db = db
    operations = DetachedOperations(response_seconds=0.01)
    command.orchestrator = SimpleNamespace(_detached_operations=operations)

    first = await command._cmd_integration_reserve_owner({"task_id": "producer"})
    again = await command._cmd_integration_reserve_owner({"task_id": "producer"})

    assert (first["success"], first["outcome"]) == (True, "running")
    assert first["poll"] == "aq integration reserve-owner --task-id producer"
    assert again["operation_id"] == first["operation_id"] and calls == ["producer"]
    assert await _owner(db) is None
    gate.set()
    operations.response_seconds = 30.0  # the poll joins the same run and waits for it
    collected = await command._cmd_integration_reserve_owner({"task_id": "producer"})
    assert (collected["success"], collected["outcome"]) == (True, "acquired")
    assert calls == ["producer"] and (await _owner(db))["owner_id"] == "producer"


async def test_detached_failure_propagates_and_is_not_retained():
    operations = DetachedOperations(response_seconds=1.0)

    async def broken():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await operations.run("k", broken, poll="retry")
    assert operations._runs == {}
