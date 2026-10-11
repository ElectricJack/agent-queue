"""A container's displayed activity, rolled up from its descendants (bright-nexus-99).

An epic's stored lifecycle stays ``IN_PROGRESS`` while any child is unfinished;
`src.task_graph.activity` derives what the dashboard shows instead, on read.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.commands.handler import CommandHandler
from src.models import Project, Task, TaskStatus
from src.task_graph.activity import load_activity, rollup


def _epic(*children: dict, own_status: str | None = "IN_PROGRESS") -> dict:
    return rollup("epic", children, own_status=own_status)


@pytest.mark.parametrize(
    ("status", "extra", "state", "reason"),
    [
        ("DEFINED", {}, "waiting", "Waiting for work to become ready"),
        ("READY", {}, "waiting", "Waiting for a worker"),
        ("PAUSED", {}, "waiting", "Paused"),
        ("READY", {"held": True}, "waiting", "Paused"),
        ("READY", {"human_gate": True}, "waiting", "Needs you"),
        ("WAITING_INPUT", {}, "waiting", "Needs you"),
        ("DEFINED", {"blocker_id": "prerequisite"}, "waiting", "Waiting on prerequisite"),
        ("READY", {"gated": True}, "waiting", "Waiting on dependencies or gates"),
        ("BLOCKED", {}, "blocked", "All unfinished descendants"),
        ("FAILED", {}, "blocked", "All unfinished descendants"),
        ("IN_PROGRESS", {}, "active", "1 descendant working"),
        ("ASSIGNED", {}, "active", "1 descendant working"),
        ("READY", {"assigned_agent_id": "worker"}, "active", "1 descendant working"),
    ],
)
def test_leaf_state_replaces_the_container_lifecycle(status, extra, state, reason):
    answer = _epic({"id": "epic.1", "parent_task_id": "epic", "status": status, **extra})
    assert answer["display_state"] == state
    assert reason in answer["display_reason"]
    assert answer["child_state_counts"] == {state: 1}
    assert answer["descendant_state_counts"] == {status: 1}


def test_nested_rollup_ignores_a_running_container_and_counts_each_leaf_once():
    phase = {"id": "phase", "parent_task_id": "epic", "status": "IN_PROGRESS"}
    leaves = [
        {"id": "leaf", "parent_task_id": "phase", "status": "DEFINED"},
        {"id": "failed", "parent_task_id": "phase", "status": "FAILED"},
        {"id": "done", "parent_task_id": "phase", "status": "COMPLETED"},
    ]
    waiting = _epic(phase, *leaves)
    assert waiting["display_state"] == "waiting"
    assert waiting["child_state_counts"] == {"waiting": 1}
    assert waiting["descendant_state_counts"] == {"COMPLETED": 1, "DEFINED": 1, "FAILED": 1}
    active = _epic(phase, {**leaves[0], "status": "IN_PROGRESS"}, *leaves[1:])
    assert active["display_state"] == "active"
    assert active["child_state_counts"] == {"active": 1}
    assert _epic(phase, {**leaves[0], "status": "BLOCKED"}, *leaves[1:])["display_state"] == "blocked"


def test_a_gate_on_a_waiting_phase_explains_its_queued_leaves():
    phase = {"id": "phase", "parent_task_id": "epic", "status": "IN_PROGRESS", "human_gate": True}
    leaf = {"id": "leaf", "parent_task_id": "phase", "status": "DEFINED"}
    answer = _epic(phase, leaf)
    assert answer["display_state"] == "waiting"
    assert answer["display_reason"] == "Needs you: waiting on a decision"


def test_one_paused_child_among_queued_work_is_named_without_claiming_all_paused():
    answer = _epic(
        {"id": "a", "parent_task_id": "epic", "status": "PAUSED"},
        {"id": "b", "parent_task_id": "epic", "status": "DEFINED"},
    )
    assert answer["display_reason"] == "Some work is paused"


@pytest.mark.parametrize("own_status", ["PAUSED", "BLOCKED", "COMPLETED"])
def test_a_container_outside_a_working_lifecycle_keeps_its_stored_status(own_status):
    answer = _epic({"id": "a", "parent_task_id": "epic", "status": "DEFINED"}, own_status=own_status)
    assert answer["display_state"] is None
    assert answer["child_state_counts"] == {"waiting": 1}


def test_finished_subtree_and_leaf_tasks_carry_no_display_state():
    done = _epic({"id": "a", "parent_task_id": "epic", "status": "COMPLETED"})
    assert done["display_state"] is None
    assert done["display_reason"] == "All descendants finished"
    assert rollup("leaf", []) == {}


# ---------------------------------------------------------------------------
# One statement per read, shared by every command surface
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(reuse_database):
    database = await reuse_database("task-graph-activity.db")
    await database.create_project(Project(id="p", name="activity project"))
    yield database


async def _tree(db, *nodes: tuple[str, str | None, TaskStatus]) -> None:
    for task_id, parent_id, status in nodes:
        await db.create_task(Task(id=task_id, project_id="p", title=task_id, description="", status=status))
        if parent_id:
            async with db._engine.begin() as conn:
                await db.set_parent(task_id, parent_id, conn=conn)


async def test_command_reads_share_the_nested_rollup_without_lifecycle_changes(db, tmp_path):
    await _tree(
        db,
        ("epic", None, TaskStatus.IN_PROGRESS),
        ("phase", "epic", TaskStatus.IN_PROGRESS),
        ("leaf", "phase", TaskStatus.DEFINED),
    )
    await db.create_gate(project_id="p", gate_type="human", title="Needs review", waiter_task_ids=["phase"])
    orch = MagicMock(db=db)
    orch._emit_notify = AsyncMock()
    handler = CommandHandler(orch, MagicMock(vault_root=str(tmp_path / "vault")))
    handler._active_project_id = None
    detail = await handler._cmd_get_task({"task_id": "epic"})
    listed = await handler._cmd_list_tasks({"project_id": "p"})
    progress = await handler._cmd_task_progress({"task_id": "epic"})
    rows = {row["id"]: row for row in listed["tasks"]}
    for answer in (detail, rows["epic"], progress):
        assert answer["display_state"] == "waiting"
        assert answer["child_state_counts"] == {"waiting": 1}
        assert answer["descendant_state_counts"] == {"DEFINED": 1}
        assert "Needs you" in answer["display_reason"]
    assert rows["phase"]["display_state"] == "waiting"
    assert "display_state" not in rows["leaf"]
    assert (await db.get_task("epic")).status == TaskStatus.IN_PROGRESS
    assert (await db.get_task("phase")).status == TaskStatus.IN_PROGRESS


async def test_load_activity_names_an_unfinished_predecessor_and_a_hold(db):
    await _tree(
        db,
        ("prereq", None, TaskStatus.IN_PROGRESS),
        ("epic", None, TaskStatus.IN_PROGRESS),
        ("leaf", "epic", TaskStatus.DEFINED),
        ("held-epic", None, TaskStatus.IN_PROGRESS),
        ("held", "held-epic", TaskStatus.READY),
    )
    await db.add_dependency("leaf", "prereq")
    await db.add_task_label("held", "hold:operator")
    activity = await load_activity(db, ["epic", "held-epic", "prereq"])
    assert set(activity) == {"epic", "held-epic"}
    assert activity["epic"]["display_reason"] == "Waiting on prereq"
    assert activity["held-epic"]["display_reason"] == "Paused"
    await db.update_task("prereq", status=TaskStatus.COMPLETED)
    assert (await load_activity(db, ["epic"]))["epic"]["display_reason"] != "Waiting on prereq"
