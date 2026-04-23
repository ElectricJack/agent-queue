"""Unit tests for ``_cmd_get_stuck_tasks`` — the CommandHandler entry point
that powers the ``get_stuck_tasks`` tool used by the
``system-health-check`` playbook.

See ``src/prompts/example_playbooks/system-health-check.md`` and
``src/database/queries/dependency_queries.py::get_stuck_active_tasks``.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig, DiscordConfig
from src.database import Database
from src.models import Project, Task, TaskStatus
from src.orchestrator import Orchestrator


PROJECT_ID = "proj"


def _task(
    task_id: str,
    *,
    project_id: str = PROJECT_ID,
    status: TaskStatus = TaskStatus.DEFINED,
    assigned_agent_id: str | None = None,
) -> Task:
    return Task(
        id=task_id,
        project_id=project_id,
        title=task_id,
        description="d",
        status=status,
        assigned_agent_id=assigned_agent_id,
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="Test Project"))
    yield database
    await database.close()


@pytest.fixture
def config(tmp_path):
    return AppConfig(
        discord=DiscordConfig(bot_token="test-token", guild_id="123"),
        workspace_dir=str(tmp_path / "workspaces"),
        database_path=str(tmp_path / "test.db"),
        data_dir=str(tmp_path / "data"),
    )


@pytest.fixture
async def handler(db, config):
    orchestrator = Orchestrator(config)
    orchestrator.db = db
    orchestrator.git = MagicMock()
    return CommandHandler(orchestrator, config)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_stuck_tasks_returns_empty_list(handler):
    """Empty queue — no stuck tasks."""
    result = await handler.execute("get_stuck_tasks", {})
    assert "error" not in result
    assert result["stuck"] == []
    assert "now_used" in result
    assert "thresholds" in result


@pytest.mark.asyncio
async def test_default_thresholds_returned_in_response(handler):
    """The response echoes the thresholds it applied (defaults: 30m/2h)."""
    result = await handler.execute("get_stuck_tasks", {})
    assert result["thresholds"]["assigned"] == 1800
    assert result["thresholds"]["in_progress"] == 7200


@pytest.mark.asyncio
async def test_fresh_assigned_task_not_stuck(handler, db):
    """A task just moved to ASSIGNED is not stuck yet."""
    await db.create_task(_task("t-1", status=TaskStatus.ASSIGNED))
    result = await handler.execute("get_stuck_tasks", {})
    assert result["stuck"] == []


@pytest.mark.asyncio
async def test_stuck_assigned_task_surfaced(handler, db):
    """An ASSIGNED task older than 30 minutes is surfaced with all fields."""
    # Seed an agent so the FK constraint on assigned_agent_id is satisfied.
    from src.models import Agent, AgentState

    await db.create_agent(
        Agent(id="agent-1", name="agent-1", agent_type="coding", state=AgentState.IDLE)
    )
    await db.create_task(
        _task("t-stuck", status=TaskStatus.ASSIGNED, assigned_agent_id="agent-1")
    )

    future_now = time.time() + 31 * 60  # 31 minutes later
    result = await handler.execute("get_stuck_tasks", {"now": future_now})

    assert "error" not in result
    assert len(result["stuck"]) == 1
    entry = result["stuck"][0]
    assert entry["id"] == "t-stuck"
    assert entry["project_id"] == PROJECT_ID
    assert entry["status"] == "ASSIGNED"
    assert entry["assigned_agent"] == "agent-1"
    assert "updated_at" in entry
    assert "seconds_in_state" in entry
    assert entry["seconds_in_state"] >= 1860  # at least 31 minutes
    assert result["now_used"] == future_now


@pytest.mark.asyncio
async def test_stuck_in_progress_task_surfaced(handler, db):
    """An IN_PROGRESS task older than 2 hours is surfaced."""
    await db.create_task(_task("t-long", status=TaskStatus.IN_PROGRESS))

    future_now = time.time() + (2 * 3600 + 60)  # 2h 1min later
    result = await handler.execute("get_stuck_tasks", {"now": future_now})

    assert len(result["stuck"]) == 1
    assert result["stuck"][0]["id"] == "t-long"
    assert result["stuck"][0]["status"] == "IN_PROGRESS"


@pytest.mark.asyncio
async def test_project_id_filter_narrows_results(handler, db):
    """``project_id`` arg restricts results to a single project."""
    # Second project with its own stuck task.
    await db.create_project(Project(id="other", name="Other"))
    await db.create_task(_task("a", project_id=PROJECT_ID, status=TaskStatus.ASSIGNED))
    await db.create_task(_task("b", project_id="other", status=TaskStatus.ASSIGNED))

    future_now = time.time() + 60 * 60  # 1h later
    result = await handler.execute(
        "get_stuck_tasks", {"now": future_now, "project_id": PROJECT_ID}
    )

    ids = {t["id"] for t in result["stuck"]}
    assert ids == {"a"}


@pytest.mark.asyncio
async def test_threshold_overrides_respected(handler, db):
    """Caller may override both thresholds — short windows surface more."""
    await db.create_task(_task("t-new", status=TaskStatus.ASSIGNED))

    # 10s later with a 5s ASSIGNED threshold: stuck.
    now_soon = time.time() + 10
    result = await handler.execute(
        "get_stuck_tasks",
        {
            "now": now_soon,
            "assigned_threshold_seconds": 5,
            "in_progress_threshold_seconds": 5,
        },
    )

    assert len(result["stuck"]) == 1
    assert result["stuck"][0]["id"] == "t-new"
    assert result["thresholds"]["assigned"] == 5
    assert result["thresholds"]["in_progress"] == 5


@pytest.mark.asyncio
async def test_non_active_statuses_ignored(handler, db):
    """READY/DEFINED/COMPLETED tasks never appear, regardless of age."""
    await db.create_task(_task("t-def", status=TaskStatus.DEFINED))
    await db.create_task(_task("t-ready", status=TaskStatus.READY))
    await db.create_task(_task("t-done", status=TaskStatus.COMPLETED))
    await db.create_task(_task("t-block", status=TaskStatus.BLOCKED))

    # Even hours in the future.
    future_now = time.time() + 24 * 3600
    result = await handler.execute("get_stuck_tasks", {"now": future_now})
    assert result["stuck"] == []


@pytest.mark.asyncio
async def test_seconds_in_state_uses_now_arg(handler, db):
    """`seconds_in_state` is ``now - updated_at`` so it's deterministic."""
    await db.create_task(_task("t-a", status=TaskStatus.ASSIGNED))

    future_now = time.time() + 5000  # ~83 minutes
    result = await handler.execute("get_stuck_tasks", {"now": future_now})

    assert len(result["stuck"]) == 1
    delta = result["stuck"][0]["seconds_in_state"]
    # updated_at was stamped at create, so delta should be close to 5000.
    # Allow a small fudge for the round-trip.
    assert 4990 <= delta <= 5010
