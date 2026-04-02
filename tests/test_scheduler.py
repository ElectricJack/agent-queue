import pytest
from src.models import (
    Project, Task, Agent, TaskStatus, AgentState, ProjectStatus, TaskType,
)
from src.scheduler import Scheduler, SchedulerState, AssignAction


def make_project(id="p-1", name="alpha", weight=1.0, max_agents=2, **kw):
    return Project(id=id, name=name, credit_weight=weight,
                   max_concurrent_agents=max_agents, **kw)


def make_task(id="t-1", project_id="p-1", status=TaskStatus.READY, priority=100, **kw):
    return Task(id=id, project_id=project_id, title=f"Task {id}",
                description="test", status=status, priority=priority, **kw)


def make_agent(id="a-1", name="claude-1", state=AgentState.IDLE, **kw):
    return Agent(id=id, name=name, agent_type="claude", state=state, **kw)


class TestScheduler:
    def test_assign_single_task_to_idle_agent(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert isinstance(actions[0], AssignAction)
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_no_idle_agents_no_actions(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent(state=AgentState.BUSY)],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_no_ready_tasks_no_actions(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task(status=TaskStatus.IN_PROGRESS)],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_proportional_allocation_favors_deficit(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha", weight=3.0),
                make_project(id="p-2", name="beta", weight=1.0),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[make_agent()],
            project_token_usage={"p-1": 60000, "p-2": 40000},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        # p-1 target=75%, actual=60% (deficit=15%)
        # p-2 target=25%, actual=40% (surplus=15%)
        assert actions[0].task_id == "t-1"

    def test_min_task_guarantee(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha", weight=19.0),
                make_project(id="p-2", name="beta", weight=1.0),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[make_agent()],
            project_token_usage={"p-1": 95000, "p-2": 0},
            project_active_agent_counts={},
            tasks_completed_in_window={"p-1": 10, "p-2": 0},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-2"  # min guarantee for p-2

    def test_respects_max_concurrent_agents(self):
        state = SchedulerState(
            projects=[make_project(max_agents=1)],
            tasks=[make_task(id="t-1"), make_task(id="t-2")],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_token_usage={},
            project_active_agent_counts={"p-1": 1},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0  # already at max

    def test_paused_project_skipped(self):
        state = SchedulerState(
            projects=[make_project(status=ProjectStatus.PAUSED)],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_priority_ordering(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(id="t-low", priority=200),
                make_task(id="t-high", priority=10),
            ],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert actions[0].task_id == "t-high"

    def test_global_budget_exhausted_stops_all(self):
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            global_budget=100000,
            global_tokens_used=100000,
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_per_project_budget_exhausted(self):
        state = SchedulerState(
            projects=[make_project(budget_limit=50000)],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={"p-1": 50000},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_multiple_agents_assigned_to_different_projects(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha"),
                make_project(id="p-2", name="beta"),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        task_ids = {a.task_id for a in actions}
        assert task_ids == {"t-1", "t-2"}

    def test_skip_project_with_zero_available_workspaces(self):
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha"),
                make_project(id="p-2", name="beta"),
            ],
            tasks=[
                make_task(id="t-1", project_id="p-1"),
                make_task(id="t-2", project_id="p-2"),
            ],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            project_available_workspaces={"p-1": 0, "p-2": 1},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].project_id == "p-2"

    def test_workspace_check_skipped_when_dict_empty(self):
        """When project_available_workspaces is empty (default), no filtering."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            project_available_workspaces={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1


class TestWorkspaceAffinity:
    def test_subtask_scheduled_when_preferred_workspace_free(self):
        """Task with preferred_workspace_id should be assigned when that workspace is unlocked."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task(preferred_workspace_id="ws-1")],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            workspace_locks={"ws-1": None},  # unlocked
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"

    def test_subtask_skipped_when_preferred_workspace_locked(self):
        """Task with preferred_workspace_id should NOT be assigned when workspace is locked."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task(preferred_workspace_id="ws-1")],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            workspace_locks={"ws-1": "t-other"},  # locked by another task
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_non_affinity_task_still_scheduled(self):
        """Task without preferred_workspace_id is always eligible."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],  # no preferred_workspace_id
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            workspace_locks={"ws-1": "t-other"},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1

    def test_backward_compat_empty_workspace_locks(self):
        """Empty workspace_locks dict means no affinity filtering."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task(preferred_workspace_id="ws-1")],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            workspace_locks={},  # empty = no filtering
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1

    def test_affinity_task_skipped_other_task_selected(self):
        """When affinity task is blocked, a non-affinity task should be selected instead."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(id="t-affinity", priority=10, preferred_workspace_id="ws-1"),
                make_task(id="t-normal", priority=50),
            ],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            workspace_locks={"ws-1": "t-other"},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-normal"


class TestSyncTaskExclusivity:
    """SYNC tasks block all other tasks from being scheduled for the same project."""

    def test_ready_sync_blocks_regular_tasks(self):
        """When a SYNC task is READY, only the SYNC task should be scheduled."""
        state = SchedulerState(
            projects=[make_project(max_agents=3)],
            tasks=[
                make_task(id="t-sync", priority=1, task_type=TaskType.SYNC),
                make_task(id="t-regular1", priority=50),
                make_task(id="t-regular2", priority=100),
            ],
            agents=[make_agent(id="a-1"), make_agent(id="a-2"), make_agent(id="a-3")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-sync"

    def test_assigned_sync_blocks_new_tasks(self):
        """When a SYNC task is ASSIGNED (not yet executing), no new tasks should start."""
        state = SchedulerState(
            projects=[make_project(max_agents=3)],
            tasks=[
                make_task(id="t-sync", priority=1, task_type=TaskType.SYNC,
                          status=TaskStatus.ASSIGNED),
                make_task(id="t-regular", priority=50),
            ],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_in_progress_sync_blocks_new_tasks(self):
        """When a SYNC task is IN_PROGRESS, no new tasks should start."""
        state = SchedulerState(
            projects=[make_project(max_agents=3)],
            tasks=[
                make_task(id="t-sync", priority=1, task_type=TaskType.SYNC,
                          status=TaskStatus.IN_PROGRESS),
                make_task(id="t-regular", priority=50),
            ],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_sync_does_not_block_other_projects(self):
        """A SYNC task in project A should not block tasks in project B."""
        state = SchedulerState(
            projects=[
                make_project(id="p-1", name="alpha"),
                make_project(id="p-2", name="beta"),
            ],
            tasks=[
                make_task(id="t-sync", project_id="p-1", priority=1,
                          task_type=TaskType.SYNC),
                make_task(id="t-regular", project_id="p-2", priority=50),
            ],
            agents=[make_agent(id="a-1"), make_agent(id="a-2")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        task_ids = {a.task_id for a in actions}
        assert task_ids == {"t-sync", "t-regular"}

    def test_completed_sync_does_not_block(self):
        """A completed SYNC task should not block new tasks."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(id="t-sync", priority=1, task_type=TaskType.SYNC,
                          status=TaskStatus.COMPLETED),
                make_task(id="t-regular", priority=50),
            ],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-regular"
