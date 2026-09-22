"""Dock markers from live attempts, not ``agents.current_task_id``.

``list_live_task_workers`` is the source of truth for the task graph's
worker markers: one row per agent that has a live, non-stale attempt on a
task still ``ASSIGNED`` or ``IN_PROGRESS`` in the given project.
"""

from uuid import uuid4

import pytest
from sqlalchemy import insert

from src.database import Database
from src.database.queries.task_session_queries import DEFAULT_STALE_AFTER
from src.database.tables import task_session_attempts
from src.models import Project, SessionRecord, Task, TaskStatus
from tests.db_fixtures import lease_dsn

NOW = 2_000_000.0


@pytest.fixture
async def db():
    database = Database(lease_dsn("task_session.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Agent Queue"))
    await database.create_project(Project(id="other", name="Other"))
    yield database
    await database.close()


async def make_task(db, task_id, *, project_id="p", status=TaskStatus.IN_PROGRESS):
    await db.create_task(
        Task(id=task_id, project_id=project_id, title=task_id, description="", status=status)
    )


async def add_session(
    db,
    session_id,
    *,
    state="running",
    last_activity=NOW - 30,
    started_at=NOW - 2 * 3600,
    ended_at=None,
    project_id="p",
    task_id=None,
):
    await db.create_session(
        SessionRecord(
            id=session_id,
            project_id=project_id,
            profile_id="worker",
            harness="claude",
            provider="tmux",
            name=f"s-{session_id}",
            lifecycle="pool",
            work_dir="/w",
            epoch="e",
            instance_token=session_id,
            started_at=started_at,
            task_id=task_id,
            state=state,
            last_activity=last_activity,
            ended_at=ended_at,
        )
    )


async def add_attempt(
    db,
    task_id,
    *,
    session_id,
    agent_id="a1",
    agent_name="Worker",
    started_at=NOW - 60,
    ended_at=None,
    project_id="p",
    state="running",
):
    attempt_id = uuid4().hex
    async with db._engine.begin() as conn:
        await conn.execute(
            insert(task_session_attempts).values(
                id=attempt_id,
                session_id=session_id,
                task_id=task_id,
                project_id=project_id,
                agent_id=agent_id,
                agent_name=agent_name,
                profile_id="worker",
                name="worker",
                lifecycle="pool",
                model="claude-opus-5",
                harness="claude",
                provider="tmux",
                state=state,
                work_dir="/w",
                started_at=started_at,
                session_started_at=started_at,
                ended_at=ended_at,
            )
        )
    return attempt_id


async def workers(db, project_id="p"):
    return await db.list_live_task_workers(project_id, now=NOW)


async def running_target(db, project_ids=("p",)):
    return await db.get_running_task_target(list(project_ids), now=NOW)


async def test_a_running_session_and_open_attempt_on_an_in_progress_task_is_returned(db):
    await make_task(db, "t1", status=TaskStatus.IN_PROGRESS)
    await add_session(db, "s1", task_id="t1")
    await add_attempt(db, "t1", session_id="s1")
    assert await workers(db) == [{"id": "a1", "name": "Worker", "current_task_id": "t1"}]


async def test_same_attempt_but_task_completed_is_not_returned(db):
    await make_task(db, "t1", status=TaskStatus.COMPLETED)
    await add_session(db, "s1", task_id="t1")
    await add_attempt(db, "t1", session_id="s1")
    assert await workers(db) == []


async def test_attempt_ended_at_set_is_not_returned(db):
    await make_task(db, "t1", status=TaskStatus.IN_PROGRESS)
    await add_session(db, "s1", task_id="t1")
    await add_attempt(db, "t1", session_id="s1", ended_at=NOW - 10)
    assert await workers(db) == []


async def test_session_ended_is_not_returned(db):
    await make_task(db, "t1", status=TaskStatus.IN_PROGRESS)
    await add_session(db, "s1", task_id="t1", state="ended")
    await add_attempt(db, "t1", session_id="s1")
    assert await workers(db) == []


async def test_stale_session_activity_and_start_are_not_returned(db):
    await make_task(db, "t1", status=TaskStatus.IN_PROGRESS)
    stale = NOW - DEFAULT_STALE_AFTER - 100
    await add_session(db, "s1", task_id="t1", last_activity=stale, started_at=stale)
    await add_attempt(db, "t1", session_id="s1", started_at=stale)
    assert await workers(db) == []


async def test_another_projects_attempt_is_not_returned(db):
    await make_task(db, "t1", project_id="other", status=TaskStatus.IN_PROGRESS)
    await add_session(db, "s1", task_id="t1", project_id="other")
    await add_attempt(db, "t1", session_id="s1", project_id="other")
    assert await workers(db) == []
    assert await workers(db, project_id="other") == [
        {"id": "a1", "name": "Worker", "current_task_id": "t1"}
    ]


async def test_task_assigned_status_is_also_returned(db):
    await make_task(db, "t1", status=TaskStatus.ASSIGNED)
    await add_session(db, "s1", task_id="t1")
    await add_attempt(db, "t1", session_id="s1")
    assert await workers(db) == [{"id": "a1", "name": "Worker", "current_task_id": "t1"}]


async def test_running_target_breaks_priority_and_start_ties_by_task_id(db):
    for task_id in ("b-task", "a-task"):
        await db.create_task(
            Task(
                id=task_id, project_id="p", title=task_id, description="",
                status=TaskStatus.IN_PROGRESS, priority=80,
            )
        )
    await add_session(db, "s-a", task_id="a-task")
    await add_session(db, "s-b", task_id="b-task")
    await add_attempt(db, "a-task", session_id="s-a", started_at=NOW - 90)
    await add_attempt(db, "b-task", session_id="s-b", started_at=NOW - 90)

    assert await running_target(db) == {
        "task_id": "a-task", "project_id": "p", "parent_task_id": None,
        "started_at": NOW - 90,
    }


async def test_no_agent_id_on_the_attempt_is_not_returned(db):
    await make_task(db, "t1", status=TaskStatus.IN_PROGRESS)
    await add_session(db, "s1", task_id="t1")
    await add_attempt(db, "t1", session_id="s1", agent_id=None)
    assert await workers(db) == []
