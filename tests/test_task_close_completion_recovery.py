"""A restart between task close's terminal transition and its completion
record must not lose the agent's account (fair-ridge-26)."""

from __future__ import annotations

import pytest

from src.commands.handler import CommandHandler
from src.config import AppConfig
from src.database import Database
from src.database.queries.result_queries import PENDING_COMPLETION_KEY
from src.models import Project, Task, TaskStatus
from tests.db_fixtures import lease_dsn
from tests.test_task_close_summary_enforcement import _StubOrchestrator


class _Restart(Exception):
    """Stands in for the daemon dying mid-close."""


class _CrashAfterTransition(_StubOrchestrator):
    async def complete_session_task(self, task, **kwargs):
        await super().complete_session_task(task, **kwargs)
        raise _Restart()


class _CrashBeforeTransition(_StubOrchestrator):
    async def complete_session_task(self, task, **kwargs):
        raise _Restart()


@pytest.fixture
async def db(tmp_path):
    database = Database(lease_dsn("close_recovery.db"))
    await database.initialize()
    yield database
    await database.close()


def _handler(db, orch_cls):
    cfg = AppConfig()
    cfg.messages.enabled = False
    return CommandHandler(orch_cls(db), cfg)


async def _in_progress(db) -> str:
    await db.create_project(Project(id="p", name="P"))
    await db.create_task(Task(id="t1", project_id="p", title="t", description="d"))
    await db.transition_task("t1", TaskStatus.IN_PROGRESS, context="test")
    return "t1"


CLOSE_ARGS = {
    "task_id": "t1",
    "outcome": "pass",
    "summary": "did the thing",
    "tests": ["aq test tests/test_x.py"],
    "commands": ["ruff check src/x.py"],
    "commit": "a" * 40,
}


async def test_successful_close_leaves_no_draft(db):
    tid = await _in_progress(db)
    result = await _handler(db, _StubOrchestrator).execute("task_close", dict(CLOSE_ARGS))
    assert result["success"] is True
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    assert len(await db.get_task_completions(tid)) == 1
    assert await db.recover_pending_completions() == []


async def test_restart_after_transition_recovers_submitted_record(db):
    tid = await _in_progress(db)
    # The handler reports the escaped exception; nothing after it ran.
    result = await _handler(db, _CrashAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    assert result.get("success") is not True
    assert (await db.get_task(tid)).status == TaskStatus.COMPLETED
    assert await db.get_task_completion(tid) is None

    assert await db.recover_pending_completions() == [tid]

    record = await db.get_task_completion(tid)
    assert record is not None
    assert record.outcome == "pass"
    assert record.summary == "did the thing"
    assert record.tests == ["aq test tests/test_x.py"]
    assert record.commands == ["ruff check src/x.py"]
    assert record.commits == ["a" * 40]
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None
    # A second start is a no-op: no duplicate record.
    assert await db.recover_pending_completions() == []
    assert len(await db.get_task_completions(tid)) == 1


async def test_restart_before_transition_drops_draft(db):
    tid = await _in_progress(db)
    # The handler reports the escaped exception; nothing after it ran.
    result = await _handler(db, _CrashBeforeTransition).execute("task_close", dict(CLOSE_ARGS))
    assert result.get("success") is not True
    assert (await db.get_task(tid)).status == TaskStatus.IN_PROGRESS

    assert await db.recover_pending_completions() == []
    assert await db.get_task_completion(tid) is None
    assert await db.get_task_meta(tid, PENDING_COMPLETION_KEY) is None


async def test_recovery_skips_when_a_record_already_landed(db):
    tid = await _in_progress(db)
    # The handler reports the escaped exception; nothing after it ran.
    result = await _handler(db, _CrashAfterTransition).execute("task_close", dict(CLOSE_ARGS))
    assert result.get("success") is not True
    draft = await db.get_task_meta(tid, PENDING_COMPLETION_KEY)
    from src.models import TaskCompletion

    await db.save_task_completion(
        TaskCompletion(
            id="repair-receipt", task_id=tid, outcome="pass",
            completed_at=draft["completed_at"] + 1,
        )
    )
    assert await db.recover_pending_completions() == []
    assert [c.id for c in await db.get_task_completions(tid)] == ["repair-receipt"]
