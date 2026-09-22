"""Dock markers from live attempts, not ``agents.current_task_id``.

``list_live_task_workers`` is the source of truth for the task graph's
worker markers: one row per agent that has a live, non-stale attempt on a
task still ``ASSIGNED`` or ``IN_PROGRESS`` in the given project.
"""

import pytest

from src.database import Database
from src.database.queries.task_session_queries import DEFAULT_STALE_AFTER
from src.models import Project, TaskStatus
from tests.db_fixtures import lease_dsn, seed_task_session_attempt

NOW = 2_000_000.0


@pytest.fixture
async def db():
    database = Database(lease_dsn("task_session.db"))
    await database.initialize()
    await database.create_project(Project(id="p", name="Agent Queue"))
    await database.create_project(Project(id="other", name="Other"))
    yield database
    await database.close()


async def workers(db, project_id="p"):
    return await db.list_live_task_workers(project_id, now=NOW)


async def test_a_running_session_and_open_attempt_on_an_in_progress_task_is_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.IN_PROGRESS, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60,
    )
    assert await workers(db) == [{"id": "a1", "name": "Worker", "current_task_id": "t1"}]


async def test_same_attempt_but_task_completed_is_not_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.COMPLETED, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60,
    )
    assert await workers(db) == []


async def test_attempt_ended_at_set_is_not_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.IN_PROGRESS, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60, attempt_ended_at=NOW - 10,
    )
    assert await workers(db) == []


async def test_session_ended_is_not_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.IN_PROGRESS, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60, session_state="ended",
    )
    assert await workers(db) == []


async def test_stale_session_activity_and_start_are_not_returned(db):
    stale = NOW - DEFAULT_STALE_AFTER - 100
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.IN_PROGRESS, session_id="s1", now=NOW,
        heartbeat_age=DEFAULT_STALE_AFTER + 100, session_started_at=stale, attempt_started_at=stale,
    )
    assert await workers(db) == []


async def test_another_projects_attempt_is_not_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="other", task_status=TaskStatus.IN_PROGRESS, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60,
    )
    assert await workers(db) == []
    assert await workers(db, project_id="other") == [
        {"id": "a1", "name": "Worker", "current_task_id": "t1"}
    ]


async def test_task_assigned_status_is_also_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.ASSIGNED, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60,
    )
    assert await workers(db) == [{"id": "a1", "name": "Worker", "current_task_id": "t1"}]


async def test_no_agent_id_on_the_attempt_is_not_returned(db):
    await seed_task_session_attempt(
        db, task_id="t1", project_id="p", task_status=TaskStatus.IN_PROGRESS, session_id="s1", now=NOW,
        session_started_at=NOW - 2 * 3600, attempt_started_at=NOW - 60, agent_id=None,
    )
    assert await workers(db) == []
