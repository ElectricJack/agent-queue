"""Direct worker counts must follow execution provenance, not task-tree size."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from src.models import AgentState, SessionRecord, Task, TaskStatus


class AgentLookup:
    def __init__(self, *, supervisors=(), busy_workers=None, native=None):
        self.supervisors = set(supervisors)
        self.busy_workers = busy_workers or {}
        # {session_id: {"starts": n, "stops": n}} — the fold of subagent_events.
        self.native = native or {}

    async def subagent_counts_by_session(self, session_ids=None):
        if session_ids is None:
            return dict(self.native)
        return {sid: self.native[sid] for sid in session_ids if sid in self.native}

    async def get_agent(self, agent_id):
        return SimpleNamespace(
            id=agent_id, role="supervisor" if agent_id in self.supervisors else "worker",
            state=AgentState.BUSY if agent_id in self.busy_workers else AgentState.IDLE,
            current_task_id=self.busy_workers.get(agent_id),
        )


def session(sid, agent_id, *, task_id=None, state="running", harness="unsupported", hooks=False):
    return SessionRecord(
        id=sid, agent_id=agent_id, task_id=task_id, project_id="project",
        profile_id="worker", harness=harness, provider="fake", name=sid,
        lifecycle="pool", state=state, work_dir="/missing", epoch="e",
        instance_token="token", started_at=1, hooks_provisioned=hooks,
    )


def task(tid, *, creator="parent-old", worker="child", status=TaskStatus.IN_PROGRESS, parent=None):
    return Task(
        id=tid, project_id="project", title=tid, description="test", status=status,
        created_by_kind="session", created_by_id=creator,
        assigned_agent_id=worker, parent_task_id=parent,
    )


async def counts(sessions, tasks, *, supervisors=(), busy_workers=None, native=None):
    from src.agents.subagents import subagent_counts
    lookup = AgentLookup(supervisors=supervisors, busy_workers=busy_workers, native=native)
    return await subagent_counts(lookup, "parent", sessions, tasks)


def events(starts, stops):
    return {"starts": starts, "stops": stops}


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
        "subagents_spawned_total": 0,
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
        "subagents_spawned_total": 0,
    }


async def test_inactive_or_never_started_parent_has_no_active_native_execution():
    assert await counts([], []) == {
        "active_subagent_count": 0, "subagent_count_complete": True,
        "aq_subagent_count": 0, "native_subagent_count": 0,
        "subagents_spawned_total": 0,
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


# ---------------------------------------------------------------------------
# Native sub-agents — harness SubagentStart / SubagentStop hooks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("harness", ["claude", "codex"])
async def test_hooked_session_reports_starts_without_stops_as_running_children(harness):
    rows = [session("parent-live", "parent", harness=harness, hooks=True)]
    result = await counts(rows, [], native={"parent-live": events(3, 1)})
    assert result == {
        "active_subagent_count": 2, "subagent_count_complete": True,
        "aq_subagent_count": 0, "native_subagent_count": 2,
        "subagents_spawned_total": 3,
    }


async def test_native_and_aq_children_are_counted_separately_and_summed():
    rows = [
        session("parent-live", "parent", harness="claude", hooks=True),
        session("child-s", "child", task_id="a"),
    ]
    result = await counts(rows, [task("a", creator="parent-live")],
                          native={"parent-live": events(2, 0)})
    assert result["aq_subagent_count"] == 1
    assert result["native_subagent_count"] == 2
    assert result["active_subagent_count"] == 3
    assert result["subagent_count_complete"] is True


async def test_a_stop_without_a_start_clamps_at_zero_rather_than_going_negative():
    rows = [session("parent-live", "parent", harness="claude", hooks=True)]
    result = await counts(rows, [], native={"parent-live": events(0, 2)})
    assert result["native_subagent_count"] == 0
    assert result["active_subagent_count"] == 0
    assert result["subagent_count_complete"] is True


async def test_one_live_session_without_hooks_makes_the_whole_count_unknown():
    # A second harness (gemini) running beside a hooked claude session: the
    # agent's total cannot be asserted just because half of it is observable.
    rows = [
        session("parent-live", "parent", harness="claude", hooks=True),
        session("parent-other", "parent", harness="gemini", hooks=False),
    ]
    result = await counts(rows, [], native={"parent-live": events(1, 0)})
    assert result["native_subagent_count"] is None
    assert result["subagent_count_complete"] is False
    # Lifetime is still exact: a recorded start happened whether or not the
    # *current* total is knowable.
    assert result["subagents_spawned_total"] == 1


async def test_events_from_a_finished_session_leave_the_lifetime_total_but_not_the_active_one():
    rows = [
        session("parent-old", "parent", state="stopped", harness="claude", hooks=True),
        session("parent-live", "parent", harness="claude", hooks=True),
    ]
    result = await counts(
        rows, [], native={"parent-old": events(5, 5), "parent-live": events(1, 0)},
    )
    assert result["native_subagent_count"] == 1
    assert result["subagents_spawned_total"] == 6


async def test_another_agents_session_events_are_never_attributed_here():
    rows = [
        session("parent-live", "parent", harness="claude", hooks=True),
        session("stranger", "someone-else", harness="claude", hooks=True),
    ]
    result = await counts(rows, [], native={"stranger": events(9, 0)})
    assert result["native_subagent_count"] == 0
    assert result["subagents_spawned_total"] == 0


# ---------------------------------------------------------------------------
# Flock rollup
# ---------------------------------------------------------------------------


def row(profile_id, *, native, aq, spawned, complete=True):
    return {
        "profile_id": profile_id, "native_subagent_count": native,
        "aq_subagent_count": aq, "subagents_spawned_total": spawned,
        "subagent_count_complete": complete,
    }


def test_flock_rollup_sums_active_native_and_aq_across_agents_and_per_profile():
    from src.agents.subagents import flock_rollup

    result = flock_rollup([
        row("implementer", native=2, aq=1, spawned=4),
        row("implementer", native=0, aq=3, spawned=1),
        row("reviewer", native=1, aq=0, spawned=1),
    ])
    assert result["totals"] == {
        "active_total": 7, "native_total": 3, "aq_total": 4,
        "spawned_total": 6, "complete": True,
    }
    assert result["by_profile"] == [
        {"profile_id": "implementer", "active_total": 6, "native_total": 2,
         "aq_total": 4, "spawned_total": 5, "complete": True},
        {"profile_id": "reviewer", "active_total": 1, "native_total": 1,
         "aq_total": 0, "spawned_total": 1, "complete": True},
    ]


def test_one_uncovered_agent_makes_its_profile_and_the_flock_total_a_lower_bound():
    from src.agents.subagents import flock_rollup

    result = flock_rollup([
        row("implementer", native=2, aq=1, spawned=4),
        row("gemini-worker", native=None, aq=2, spawned=0, complete=False),
    ])
    assert result["totals"]["complete"] is False
    # The known half still counts — an unknown native number contributes 0,
    # never drops the AQ children we *can* see.
    assert result["totals"]["active_total"] == 5
    assert result["totals"]["aq_total"] == 3
    covered = {bucket["profile_id"]: bucket["complete"] for bucket in result["by_profile"]}
    assert covered == {"implementer": True, "gemini-worker": False}


def test_an_empty_flock_rolls_up_to_a_complete_zero():
    from src.agents.subagents import flock_rollup

    assert flock_rollup([]) == {
        "totals": {"active_total": 0, "native_total": 0, "aq_total": 0,
                   "spawned_total": 0, "complete": True},
        "by_profile": [],
    }
