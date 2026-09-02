"""``sessions.stuck_composer`` — the doctor half of the lost-Enter stall.

The live symptom (2026-09-02): a stall nudge sat in a Claude composer,
unsubmitted, and one manual ``tmux send-keys Enter`` cleared it instantly.
This check is the operator surface for exactly that: report which sessions
are holding a nudge nobody submitted, and press Enter with ``--fix``.
"""

from __future__ import annotations

import sys
import time

import pytest

import src.doctor
from src.doctor.models import Severity
from src.models import AgentProfile, Project, SessionRecord, Task, TaskStatus
from src.sessions import SessionProviderRegistry
from src.sessions.fake import FakeProvider
from src.sessions.provider import NotSubmitted, SessionHandle, SessionSpec

# ``src/doctor/__init__.py`` rebinds the package attribute ``session_checks``
# to the *factory function*, so the submodule is only reachable through
# ``sys.modules`` (see the same note in tests/test_pool_doctor.py).
session_checks = sys.modules["src.doctor.session_checks"]

PROJECT_ID = "proj"
CHECK = "sessions.stuck_composer"
NUDGE = "No progress for 8 min on task t1. Close or continue: ..."


@pytest.fixture
async def db(tmp_path):
    from src.database import Database

    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    await database.create_project(Project(id=PROJECT_ID, name="p"))
    await database.create_profile(AgentProfile(id="worker", name="w", lifecycle="pool"))
    yield database
    await database.close()


class _Handler:
    def __init__(self, provider):
        registry = SessionProviderRegistry({"fake": FakeProvider})
        registry._instances["fake"] = provider
        self.orchestrator = type(
            "_Orch", (), {"session_providers": registry, "config": None}
        )()


async def _running_session(db, provider, *, state="running"):
    await db.create_task(
        Task(
            id="t1",
            project_id=PROJECT_ID,
            title="t",
            description="d",
            status=TaskStatus.IN_PROGRESS,
        )
    )
    row = SessionRecord(
        id="s1",
        project_id=PROJECT_ID,
        profile_id="worker",
        harness="claude",
        provider="fake",
        name="p-worker--proj--abc",
        lifecycle="pool",
        work_dir="/w",
        epoch="e",
        instance_token="tok",
        started_at=time.time(),
        state=state,
        task_id="t1",
    )
    await db.create_session(row)
    await provider.start(
        SessionSpec(
            session_name=row.name,
            work_dir=row.work_dir,
            command=("agent",),
            instance_token=row.instance_token,
        )
    )
    return row


def _handle(row):
    return SessionHandle(name=row.name, provider="fake", instance_token=row.instance_token)


def test_the_check_is_registered_with_a_fix():
    check = session_checks.CHECKS[CHECK]
    assert check.owner == "session-runtime"
    assert check.fix is not None
    assert CHECK in {c.id for c in src.doctor.default_registry().checks()}


class TestStuckComposerCheck:
    async def test_clean_sessions_report_ok(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        await provider.nudge(_handle(row), NUDGE)

        result = await session_checks.run_check(db, _Handler(provider), CHECK)

        assert result.severity is Severity.OK
        assert result.fixable is True

    async def test_an_unsubmitted_nudge_is_reported_with_the_task(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        provider.swallow_next_nudge(row.name)
        with pytest.raises(NotSubmitted):
            await provider.nudge(_handle(row), NUDGE)

        result = await session_checks.run_check(db, _Handler(provider), CHECK)

        assert result.severity is Severity.WARN
        assert result.data["count"] == 1
        assert result.data["sessions"][0]["task_id"] == "t1"
        assert row.name in result.detail
        assert "t1" in result.detail

    async def test_fix_presses_enter_and_the_recheck_goes_green(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        provider.swallow_next_nudge(row.name)
        with pytest.raises(NotSubmitted):
            await provider.nudge(_handle(row), NUDGE)

        result = await session_checks.run_check(db, _Handler(provider), CHECK, repair=True)

        assert result.severity is Severity.OK
        assert result.fix_applied is True
        assert provider.sent_nudges == [(row.name, NUDGE)]

    async def test_a_check_run_without_fix_never_presses_a_key(self, db):
        provider = FakeProvider()
        row = await _running_session(db, provider)
        provider.swallow_next_nudge(row.name)
        with pytest.raises(NotSubmitted):
            await provider.nudge(_handle(row), NUDGE)

        await session_checks.run_check(db, _Handler(provider), CHECK)

        assert provider.sent_nudges == []

    async def test_no_orchestrator_is_not_an_error(self, db):
        result = await session_checks.run_check(db, None, CHECK)
        assert result.severity is Severity.OK
