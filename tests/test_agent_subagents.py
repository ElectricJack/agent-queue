"""Direct worker counts must follow execution provenance, not task-tree size."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.models import AgentState, SessionRecord, Task, TaskStatus


class AgentLookup:
    def __init__(self, *, supervisors=(), busy_workers=None):
        self.supervisors = set(supervisors)
        self.busy_workers = busy_workers or {}

    async def get_agent(self, agent_id):
        return SimpleNamespace(
            id=agent_id, role="supervisor" if agent_id in self.supervisors else "worker",
            state=AgentState.BUSY if agent_id in self.busy_workers else AgentState.IDLE,
            current_task_id=self.busy_workers.get(agent_id),
        )


def session(sid, agent_id, *, task_id=None, state="running", harness="unsupported"):
    return SessionRecord(
        id=sid, agent_id=agent_id, task_id=task_id, project_id="project",
        profile_id="worker", harness=harness, provider="fake", name=sid,
        lifecycle="pool", state=state, work_dir="/missing", epoch="e",
        instance_token="token", started_at=1,
    )


def task(tid, *, creator="parent-old", worker="child", status=TaskStatus.IN_PROGRESS, parent=None):
    return Task(
        id=tid, project_id="project", title=tid, description="test", status=status,
        created_by_kind="session", created_by_id=creator,
        assigned_agent_id=worker, parent_task_id=parent,
    )


async def counts(sessions, tasks, *, supervisors=(), busy_workers=None):
    from src.agents.subagents import subagent_counts
    lookup = AgentLookup(supervisors=supervisors, busy_workers=busy_workers)
    return await subagent_counts(lookup, "parent", sessions, tasks)


async def test_counts_active_direct_workers_from_previous_parent_sessions_once():
    rows = [
        session("parent-old", "parent", state="stopped"),
        session("child-a", "child", task_id="a"),
        session("child-b", "child", task_id="b"),
        session("other-child", "other", task_id="c"),
    ]
    result = await counts(rows, [task("a"), task("b"), task("c", worker="other")])
    assert result == {
        "active_subagent_count": 2, "subagent_count_complete": True,
        "aq_subagent_count": 2, "native_subagent_count": 0,
    }


@pytest.mark.parametrize("status", [TaskStatus.READY, TaskStatus.DEFINED, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.PAUSED])
async def test_queued_and_finished_tasks_do_not_count_even_with_stale_sessions(status):
    rows = [session("parent-old", "parent", state="stopped"), session("child-s", "child", task_id="a")]
    assert (await counts(rows, [task("a", status=status)]))["aq_subagent_count"] == 0


async def test_hierarchy_self_grandchildren_and_unassigned_containers_do_not_count():
    rows = [
        session("parent-old", "parent", task_id="self"),
        session("child-s", "child", task_id="child-task"),
        session("grandchild-s", "grandchild", task_id="grandchild-task"),
        session("unrelated-s", "unrelated", task_id="unrelated-task"),
    ]
    tasks = [
        task("self", worker="parent"), task("child-task"),
        task("grandchild-task", creator="child-s", worker="grandchild", parent="child-task"),
        task("unrelated-task", creator="someone-else", worker="unrelated", parent="self"),
        task("container", worker=None),
    ]
    assert (await counts(rows, tasks))["aq_subagent_count"] == 1


async def test_stopped_worker_and_changed_task_owner_are_not_active_children():
    rows = [session("parent-old", "parent", state="stopped"), session("child-s", "child", task_id="a")]
    assert (await counts(rows, [task("a")]))["aq_subagent_count"] == 1
    assert (await counts([rows[0], replace(rows[1], state="stopped")], [task("a")]))["aq_subagent_count"] == 0
    assert (await counts(rows, [task("a", worker="replacement")]))["aq_subagent_count"] == 0


async def test_live_waiting_worker_counts_but_supervisor_role_does_not():
    rows = [session("parent-old", "parent", state="stopped"), session("child-s", "child", task_id="a")]
    tasks = [task("a", status=TaskStatus.WAITING_INPUT)]
    assert (await counts(rows, tasks))["aq_subagent_count"] == 1
    assert (await counts(rows, tasks, supervisors={"child"}))["aq_subagent_count"] == 0


async def test_unlinked_legacy_execution_uses_the_task_agent_identity():
    rows = [session("parent-old", "parent", state="stopped"), session("legacy", None, task_id="a")]
    assert (await counts(rows, [task("a")], busy_workers={"child": "a"}))["aq_subagent_count"] == 1


@pytest.mark.parametrize("harness", ["claude", "codex", "unknown"])
async def test_live_parent_without_native_lifecycle_telemetry_is_not_a_fake_zero(harness):
    rows = [session("parent-old", "parent", harness=harness), session("child-s", "child", task_id="a")]
    assert await counts(rows, [task("a")]) == {
        "active_subagent_count": None, "subagent_count_complete": False,
        "aq_subagent_count": 1, "native_subagent_count": None,
    }


async def test_inactive_or_never_started_parent_has_no_active_native_execution():
    assert await counts([], []) == {
        "active_subagent_count": 0, "subagent_count_complete": True,
        "aq_subagent_count": 0, "native_subagent_count": 0,
    }
    rows = [session("parent-old", "parent", state="sleeping", harness="claude")]
    assert (await counts(rows, []))["native_subagent_count"] == 0


async def test_busy_registered_worker_counts_without_a_session_only_for_its_current_task():
    rows = [session("parent-old", "parent", state="stopped")]
    assert (await counts(rows, [task("a")], busy_workers={"child": "a"}))["aq_subagent_count"] == 1
    assert (await counts(rows, [task("a")]))["aq_subagent_count"] == 0
    assert (await counts(rows, [task("a")], busy_workers={"child": "other"}))["aq_subagent_count"] == 0
    assert (await counts(rows, [task("a")], busy_workers={"child": "a"}, supervisors={"child"}))["aq_subagent_count"] == 0


async def test_task_completion_or_archival_clears_stale_active_worker_count():
    rows = [session("parent-old", "parent", state="stopped"), session("child-s", "child", task_id="a")]
    assert (await counts(rows, [task("a")], busy_workers={"child": "a"}))["aq_subagent_count"] == 1
    completed = task("a", status=TaskStatus.COMPLETED)
    assert (await counts(rows, [completed], busy_workers={"child": "a"}))["aq_subagent_count"] == 0
    # Archived tasks have left the live tasks table, even if session/agent pointers remain.
    assert (await counts(rows, [], busy_workers={"child": "a"}))["aq_subagent_count"] == 0


async def test_legacy_parent_session_uses_current_task_assignment_for_provenance():
    rows = [session("parent-old", None, task_id="parent-task"), session("child-s", "child", task_id="a")]
    tasks = [task("parent-task", creator="operator", worker="parent"), task("a")]
    result = await counts(rows, tasks, busy_workers={"parent": "parent-task"})
    assert result["aq_subagent_count"] == 1
    assert result["native_subagent_count"] is None


async def test_busy_parent_without_a_session_does_not_claim_complete_native_telemetry():
    result = await counts([], [task("parent-task", creator="operator", worker="parent")],
                          busy_workers={"parent": "parent-task"})
    assert result["active_subagent_count"] is None
    assert result["subagent_count_complete"] is False


@pytest.mark.parametrize("state", ["stopped", "running"])
async def test_unlinked_old_parent_is_not_reattributed_after_task_reassignment(state):
    rows = [
        replace(session("parent-old", None, task_id="reassigned", state=state), last_claim_epoch=1),
        session("child-s", "child", task_id="a"),
    ]
    # The parent now owns a new execution of that task; the old session has
    # no stable agent link, so its delegation must not be inferred from today.
    current = task("reassigned", creator="operator", worker="parent")
    current.claim_epoch = 2
    result = await counts(rows, [current, task("a")], busy_workers={"parent": "reassigned"})
    assert result["aq_subagent_count"] == 0


async def test_unlinked_child_session_with_conflicting_claim_does_not_count():
    rows = [session("parent-old", "parent", state="stopped"),
            replace(session("stale", None, task_id="a"), last_claim_epoch=1)]
    child = task("a")
    child.claim_epoch = 2
    assert (await counts(rows, [child]))["aq_subagent_count"] == 0
