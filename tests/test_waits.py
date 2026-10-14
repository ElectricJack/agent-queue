"""Verified-wait lifecycle vocabulary (implementation §6; services remain opt-in)."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert

from src.database.tables import continuations, wait_records

from src.api.models.task import TaskDetail, TaskDict
from src.commands.task_commands import TaskCommandsMixin
from src.database.queries.metrics_queries import _live_sessions_stmt
from src.models import (
    OPEN_TASK_STATUSES,
    SUSPENDED_TASK_STATUSES,
    Project,
    SessionRecord,
    Task,
    TaskEvent,
    TaskStatus,
)
from src.orchestrator.events import EventsMixin
from src.sessions.obsolete import ObsoleteClose
from src.state_machine import InvalidTransition, is_valid_status_transition, task_transition

SUSPENDED = (TaskStatus.WAITING, TaskStatus.HIBERNATED, TaskStatus.RESUME_PENDING)
# Independently enumerated from the approved table; all other suspended moves
# must fail, even when legacy status validation is still in warn-only mode.
MOVES = [
    (TaskStatus.IN_PROGRESS, TaskEvent.WAIT_REGISTERED, TaskStatus.WAITING),
    (TaskStatus.WAITING, TaskEvent.WAIT_ACKNOWLEDGED, TaskStatus.IN_PROGRESS),
    (TaskStatus.WAITING, TaskEvent.HIBERNATION_COMMITTED, TaskStatus.HIBERNATED),
    (TaskStatus.HIBERNATED, TaskEvent.WAIT_RESOLVED, TaskStatus.RESUME_PENDING),
    (TaskStatus.RESUME_PENDING, TaskEvent.WAIT_ACKNOWLEDGED, TaskStatus.IN_PROGRESS),
    (TaskStatus.RESUME_PENDING, TaskEvent.RESUME_HELD, TaskStatus.HIBERNATED),
    (TaskStatus.PAUSED, TaskEvent.UNPAUSE_RESUME, TaskStatus.RESUME_PENDING),
    (TaskStatus.PAUSED, TaskEvent.UNPAUSE_HIBERNATE, TaskStatus.HIBERNATED),
    *[
        (status, event, target)
        for status in SUSPENDED
        for event, target in (
            (TaskEvent.MANUAL_PAUSE, TaskStatus.PAUSED),
            (TaskEvent.WAIT_REROUTED, TaskStatus.READY),
            (TaskEvent.AGENT_COMPLETED, TaskStatus.COMPLETED),
            (TaskEvent.AGENT_FAILED, TaskStatus.FAILED),
        )
    ],
]


@pytest.mark.parametrize("source,event,target", MOVES)
def test_section_six_transition(source, event, target):
    assert task_transition(source, event) is target
    assert is_valid_status_transition(source, target)


@pytest.mark.parametrize("source", SUSPENDED)
def test_suspended_events_are_closed_vocabulary(source):
    allowed = {event for status, event, _ in MOVES if status is source}
    for event in TaskEvent:
        if event not in allowed:
            with pytest.raises(InvalidTransition) as error:
                task_transition(source, event)
            assert error.value.code == "task.invalid_transition"


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("wait-statuses.db")
    await database.create_project(Project(id="proj", name="p"))
    return database


@pytest.mark.parametrize(
    "source,target",
    [
        (source, target)
        for source in TaskStatus
        for target in TaskStatus
        if source != target
        and (source in SUSPENDED or target in SUSPENDED)
        and (source, target) not in {(s, t) for s, _, t in MOVES}
    ],
)
async def test_invalid_suspended_database_move_is_refused_without_rollout_flag(db, source, target):
    db.set_state_machine_enforcement(False)
    await db.create_task(
        Task(id="task", project_id="proj", title="t", description="", status=source)
    )
    with pytest.raises(InvalidTransition) as error:
        await db.transition_task("task", target)
    assert error.value.code == "task.invalid_transition"
    assert (await db.get_task("task")).status is source


@pytest.mark.parametrize(
    "status,state,live",
    [
        (TaskStatus.WAITING, "running", True),
        (TaskStatus.HIBERNATED, "running", False),
        (TaskStatus.HIBERNATED, "starting", False),
        (TaskStatus.RESUME_PENDING, "running", False),
        (TaskStatus.RESUME_PENDING, "starting", True),
    ],
)
async def test_hibernated_session_never_counts_as_live_even_with_stale_session_row(
    db, status, state, live
):
    await db.create_task(
        Task(id="task", project_id="proj", title="t", description="", status=status)
    )
    await db.create_session(
        SessionRecord(
            id="session",
            project_id="proj",
            profile_id="worker",
            task_id="task",
            harness="codex",
            provider="fake",
            name="worker",
            lifecycle="pool",
            state=state,
            work_dir="/tmp/test-wait-session",
            epoch="epoch",
            instance_token="token",
            started_at=1.0,
        )
    )
    async with db._engine.connect() as conn:
        rows = (await conn.execute(_live_sessions_stmt())).mappings().all()
    assert sum(row["count"] for row in rows) == int(live)
    assert status in OPEN_TASK_STATUSES
    assert status in SUSPENDED_TASK_STATUSES


@pytest.mark.parametrize("status", SUSPENDED)
async def test_unpause_never_returns_suspended_work_to_ready_or_stopped_waiting_owner(db, status):
    await db.create_task(
        Task(id="task", project_id="proj", title="t", description="", status=status)
    )
    await db.pause_task("task")
    resumed = await db.resume_task("task")
    assert resumed.status is TaskStatus.HIBERNATED  # no resolved continuation exists


@pytest.mark.parametrize("status", SUSPENDED)
async def test_status_event_carries_committed_value_live_and_in_replay(db, status):
    task = Task(id="task", project_id="proj", title="t", description="", status=status)
    await db.create_task(task)
    emitter = EventsMixin()
    emitter.bus = SimpleNamespace(emit=AsyncMock())
    commands = TaskCommandsMixin()
    commands.db = db
    commands.orchestrator = emitter
    await commands._emit_task_graph_change("task.updated", task)
    payload = emitter.bus.emit.call_args.args[1]
    assert payload["status"] == status.value
    events = await db.get_recent_events(task_id="task", event_type="task.updated")
    assert json.loads(events[0]["payload"])["status"] == status.value
    assert payload["seq"] == events[0]["id"]


def test_api_status_enumeration_advertises_wait_states_without_retiring_legacy_readers():
    for model in (TaskDetail, TaskDict):
        status_schema = model.model_json_schema()["properties"]["status"]
        assert {s.value for s in SUSPENDED}.issubset(status_schema["enum"])
        assert "WAITING_INPUT" in status_schema["enum"]


@pytest.mark.parametrize(
    "outcome,cleared,expected",
    [
        ("pending", False, TaskStatus.HIBERNATED),
        ("satisfied", False, TaskStatus.RESUME_PENDING),
        ("satisfied", True, TaskStatus.HIBERNATED),
    ],
)
async def test_explicit_unpause_revalidates_only_uncleared_continuation_outcome(
    db, outcome, cleared, expected
):
    await db.create_task(
        Task(id="task", project_id="proj", title="t", description="", status=TaskStatus.HIBERNATED)
    )
    async with db.immediate() as conn:
        await conn.execute(
            insert(wait_records).values(
                id="wait",
                project_id="proj",
                owner_kind="task",
                owner_id="task",
                origin="agent",
                condition_kind="timer",
                condition={"due_at": 3.0},
                source_key="timer:wait",
                source_generation=1,
                source_watermark_seq=0,
                verification_state="verified",
                evidence_ref="test:timer",
                verified_at=1.0,
                created_at=1.0,
                request_hash="test",
                outcome=outcome,
                cleared_at=2.0 if cleared else None,
            )
        )
        await conn.execute(
            insert(continuations).values(
                id="continuation",
                task_id="task",
                wait_id="wait",
                suspension_generation=1,
                facts={},
                branch="aq/task",
                prior_status="IN_PROGRESS",
                held_cursor_seq=0,
                resume_state="sleeping",
            )
        )
    await db.pause_task("task")
    assert (await db.resume_task("task")).status is expected


@pytest.mark.parametrize("status", SUSPENDED)
async def test_operator_one_shot_close_can_finish_suspended_work(db, status):
    await db.create_task(
        Task(id="task", project_id="proj", title="t", description="", status=status)
    )
    await ObsoleteClose(db, release_owner=None).close(
        "task", reason="operator disposition", principal="operator"
    )
    assert (await db.get_task("task")).status is TaskStatus.COMPLETED
