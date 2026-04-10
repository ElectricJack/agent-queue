import pytest
from src.models import (
    Project,
    Task,
    Agent,
    TaskStatus,
    AgentState,
    ProjectStatus,
    TaskType,
)
from src.scheduler import Scheduler, SchedulerState, AssignAction


def make_project(id="p-1", name="alpha", weight=1.0, max_agents=2, **kw):
    return Project(id=id, name=name, credit_weight=weight, max_concurrent_agents=max_agents, **kw)


def make_task(id="t-1", project_id="p-1", status=TaskStatus.READY, priority=100, **kw):
    return Task(
        id=id,
        project_id=project_id,
        title=f"Task {id}",
        description="test",
        status=status,
        priority=priority,
        **kw,
    )


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
                make_task(
                    id="t-sync", priority=1, task_type=TaskType.SYNC, status=TaskStatus.ASSIGNED
                ),
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
                make_task(
                    id="t-sync", priority=1, task_type=TaskType.SYNC, status=TaskStatus.IN_PROGRESS
                ),
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
                make_task(id="t-sync", project_id="p-1", priority=1, task_type=TaskType.SYNC),
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
                make_task(
                    id="t-sync", priority=1, task_type=TaskType.SYNC, status=TaskStatus.COMPLETED
                ),
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


class TestAgentTypeMatching:
    """Agent type matching during task assignment (Roadmap 7.3.3 / 7.3.5).

    Implements the type-matching dimension of agent affinity from
    agent-coordination spec §3 (Core Concepts): "a review task should go
    to a review agent, not a coding agent."

    Type matching is a hard constraint — tasks with an explicit agent_type
    are only assigned to agents of that same type.  Tasks without an
    agent_type requirement match any agent.
    """

    @staticmethod
    def _agent(id: str, agent_type: str, state: AgentState = AgentState.IDLE) -> Agent:
        """Create an Agent with the given type (bypasses make_agent's default)."""
        return Agent(id=id, name=f"agent-{id}", agent_type=agent_type, state=state)

    def test_type_mismatch_blocks_assignment(self):
        """(a) task with agent_type='code-review' is NOT assigned to a 'coding' agent."""
        task = make_task(id="t-1", agent_type="code-review")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_type_match_allows_assignment(self):
        """(b) task with agent_type='coding' IS assigned to an available coding agent."""
        task = make_task(id="t-1", agent_type="coding")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_no_agent_type_matches_any_agent(self):
        """(c) task with no agent_type is assigned to any available agent."""
        task = make_task(id="t-1")  # agent_type=None (default)
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_unmatched_type_stays_queued(self):
        """(d) task with agent_type that no agent matches stays queued."""
        task = make_task(id="t-1", agent_type="qa")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[
                self._agent("a-1", "coding"),
                self._agent("a-2", "code-review"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_mixed_typed_and_untyped_tasks(self):
        """Typed tasks go to matching agents; untyped tasks go to any agent."""
        typed_task = make_task(id="t-typed", agent_type="coding", priority=100)
        untyped_task = make_task(id="t-untyped", priority=100)
        state = SchedulerState(
            projects=[make_project()],
            tasks=[typed_task, untyped_task],
            agents=[self._agent("a-1", "code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        # Only the untyped task should be assigned (coding ≠ code-review)
        assert len(actions) == 1
        assert actions[0].task_id == "t-untyped"

    def test_multiple_agents_different_types(self):
        """Each typed task routes to the agent with matching type."""
        review_task = make_task(id="t-review", agent_type="code-review", priority=100)
        coding_task = make_task(id="t-coding", agent_type="coding", priority=100)
        state = SchedulerState(
            projects=[make_project()],
            tasks=[review_task, coding_task],
            agents=[
                self._agent("a-review", "code-review"),
                self._agent("a-coding", "coding"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        by_agent = {a.agent_id: a for a in actions}
        assert by_agent["a-review"].task_id == "t-review"
        assert by_agent["a-coding"].task_id == "t-coding"

    def test_type_matching_with_affinity(self):
        """Type matching is applied before affinity ordering."""
        # Task prefers agent a-1 (affinity), but a-1 has wrong type.
        # a-2 has the right type → task should go to a-2.
        task = make_task(
            id="t-1",
            agent_type="code-review",
            affinity_agent_id="a-1",
        )
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[
                self._agent("a-1", "coding"),
                self._agent("a-2", "code-review"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        # a-1 can't pick up the task (wrong type), a-2 can
        assert len(actions) == 1
        assert actions[0].agent_id == "a-2"
        assert actions[0].task_id == "t-1"

    def test_type_matching_with_affinity_and_correct_type(self):
        """Affinity agent with correct type gets priority."""
        task = make_task(
            id="t-1",
            agent_type="coding",
            affinity_agent_id="a-1",
        )
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[
                self._agent("a-1", "coding"),
                self._agent("a-2", "coding"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        # Both agents match type, but a-1 has affinity → picks task first
        a1_action = next((a for a in actions if a.agent_id == "a-1"), None)
        assert a1_action is not None
        assert a1_action.task_id == "t-1"

    def test_agent_idle_but_wrong_type_stays_idle(self):
        """An idle agent with wrong type doesn't pick up typed tasks."""
        task = make_task(id="t-1", agent_type="qa")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_type_matching_across_projects(self):
        """Type matching works correctly with multiple projects."""
        p1_task = make_task(id="t-p1", project_id="p-1", agent_type="coding")
        p2_task = make_task(id="t-p2", project_id="p-2", agent_type="code-review")
        state = SchedulerState(
            projects=[
                make_project(id="p-1"),
                make_project(id="p-2"),
            ],
            tasks=[p1_task, p2_task],
            agents=[self._agent("a-1", "code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        # Agent is code-review type → only picks up the matching task from p-2
        assert len(actions) == 1
        assert actions[0].task_id == "t-p2"
        assert actions[0].agent_id == "a-1"
