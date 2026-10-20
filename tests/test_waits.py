"""Verified-wait lifecycle vocabulary (implementation §6; services remain opt-in)."""

from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert

from src.api.models.task import TaskDetail, TaskDict
from src.commands.task_commands import TaskCommandsMixin
from src.database.queries.metrics_queries import _live_sessions_stmt
from src.database.tables import continuations, wait_records
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
from src.waits.conditions import Condition, SourceEvent, WaitError
from src.waits.registry import build_registry

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


NOW = 1_800_000_000.0
SHA = "a" * 40

CONDITIONS = [
    {"kind": "task", "task_id": "t"},
    {"kind": "dependency", "task_id": "t", "depends_on_task_id": "b"},
    {"kind": "job", "job_id": "j", "attempt": 1},
    {"kind": "ci_check", "repository_id": "r", "head_sha": SHA, "check_name": "tests"},
    {"kind": "ci_run", "repository_id": "r", "head_sha": SHA, "run_id": "123"},
    {"kind": "pull_request", "repository_id": "r", "expected_head_sha": SHA, "pr_number": 3},
    {"kind": "batch", "batch_id": "b"},
    {"kind": "message", "thread_id": "thread", "after_seq": 0},
    {"kind": "human", "subject": "review", "review_id": "rev"},
    {"kind": "human", "subject": "task_comment", "task_id": "t"},
    {"kind": "human", "subject": "message_thread", "thread_id": "thread", "after_seq": 0},
    {"kind": "timer", "due_at": NOW + 20},
    {"kind": "resource", "blocker": "slot", "identity": "p:profile", "source_revision": "1"},
    {"kind": "playbook_condition", "run_id": "run", "wait_id": "wait"},
]


@pytest.mark.parametrize("raw", CONDITIONS)
def test_normalised_conditions_round_trip_without_trusting_internal_bindings(raw):
    registry = build_registry(clock=lambda: NOW)
    cond = registry.normalise(raw)
    assert registry.normalise(cond.as_dict()) == cond
    bound = cond.bind(generation=7, project_id="p", blockers=[])
    assert Condition.from_record(bound.as_record()) == bound
    with pytest.raises(WaitError, match="typed condition"):
        registry.normalise(bound.as_record())


@pytest.mark.parametrize(
    "extra",
    [
        {"any": []},
        {"all": []},
        {"condition": {}},
        {"timeout": 30},
        {"mode": "in_place"},
        {"claimed": "CI passed"},
        {"head_sha": SHA},
    ],
)
@pytest.mark.parametrize("raw", CONDITIONS)
def test_mixed_predicates_and_agent_policy_are_refused(raw, extra):
    # head_sha is an identity field on CI, not an extra predicate there.
    if set(extra) <= set(raw):
        return
    with pytest.raises(WaitError) as error:
        build_registry().normalise({**raw, **extra})
    assert error.value.code == "wait.condition_required"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "when it is done",
        {},
        {"kind": []},
        {"kind": "review", "review_id": "r"},
        {"kind": "task", "task_id": "t", "generation": True},
        {"kind": "task", "task_id": "t", "target_statuses": "COMPLETED"},
        {"kind": "task", "task_id": "t", "target_statuses": []},
        {"kind": "task", "task_id": "t", "target_statuses": ["UNKNOWN"]},
        {"kind": "task", "task_id": "t", "target_statuses": ["CANCELLED"]},
        {"kind": "task", "task_id": False},
        {"kind": "dependency", "task_id": "a", "depends_on_task_id": "b", "dep_type": []},
        {"kind": "timer", "due_at": float("nan")},
        {"kind": "timer", "due_at": float("inf")},
        {"kind": "timer", "due_at": "2026-01-01T00:00:00"},
        {"kind": "ci_check", "repository_id": "r", "head_sha": "abc123", "check_name": "tests"},
        {"kind": "message", "thread_id": "t", "after_seq": -1},
        {"kind": "job", "job_id": "j", "attempt": 0},
        {"kind": "batch", "batch_id": "b", "predicate": "green"},
        {"kind": "resource", "blocker": "anything", "identity": "i", "source_revision": "v"},
    ],
)
def test_invalid_conditions_have_a_structured_refusal(raw):
    with pytest.raises(WaitError) as error:
        build_registry().normalise(raw)
    assert error.value.as_dict()["code"] == "wait.condition_required"


def test_registry_is_frozen_and_has_exactly_the_approved_kinds():
    registry = build_registry()
    assert set(registry.adapters) == {c["kind"] for c in CONDITIONS}
    with pytest.raises(TypeError):
        registry.adapters["unchecked"] = object()


def test_result_projection_rejects_a_new_source_generation():
    registry = build_registry()
    adapter = registry.adapter("task")
    old = registry.normalise({"kind": "task", "task_id": "t", "generation": 1})
    new = registry.normalise({"kind": "task", "task_id": "t", "generation": 2})
    event = SourceEvent(adapter.source_key(new), "rev", "result:2")
    with pytest.raises(WaitError):
        adapter.project_result(old, event)
    assert adapter.project_result(new, event) == "result:2"


@pytest.mark.parametrize(
    "raw,identity,generation",
    list(
        zip(
            CONDITIONS,
            [
                "task:t",
                "dependency:t:b:blocks",
                "job:j",
                f"ci_check:r:{SHA}:tests",
                f"ci_run:r:{SHA}:123",
                f"pull_request:r:3:{SHA}",
                "batch:b",
                "message:thread",
                "review:rev",
                "human:task_comment:t",
                "message:thread",
                f"timer:{NOW + 20}",
                "resource:slot:p:profile",
                "playbook_condition:run:wait",
            ],
            [7, 7, 7, 0, 0, 0, 7, 0, 7, 7, 0, 0, 0, 7],
            strict=True,
        )
    ),
)
def test_source_identity_contract_for_event_writers(raw, identity, generation):
    registry = build_registry(clock=lambda: NOW)
    cond = registry.normalise(raw).bind(generation=7)
    source = registry.adapter(cond.kind).source_key(cond)
    assert (source.identity, source.generation) == (identity, generation)


def test_wait_package_has_no_imports_from_command_session_or_orchestrator_packages():
    for path in Path("src/waits").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [n.name for n in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            assert not any(
                name.startswith(("src.commands", "src.sessions", "src.orchestrator"))
                for name in names
            ), path
