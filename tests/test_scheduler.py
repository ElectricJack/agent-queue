import logging
import time

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

    # ── (e) Multiple type capabilities ──────────────────────────────────

    def test_multi_type_agent_matches_first_type(self):
        """(e) Agent with comma-separated types matches a task requiring any of its types."""
        task = make_task(id="t-1", agent_type="coding")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding,code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_multi_type_agent_matches_second_type(self):
        """(e) Agent with comma-separated types matches a task requiring the second type."""
        task = make_task(id="t-1", agent_type="code-review")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding,code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"
        assert actions[0].agent_id == "a-1"

    def test_multi_type_agent_no_match(self):
        """(e) Agent with multiple types still rejects tasks requiring an unlisted type."""
        task = make_task(id="t-1", agent_type="qa")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding,code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 0

    def test_multi_type_agent_routes_different_tasks(self):
        """(e) Multi-type agent picks up tasks of any of its types across rounds."""
        coding_task = make_task(id="t-coding", agent_type="coding", priority=100)
        review_task = make_task(id="t-review", agent_type="code-review", priority=100)
        state = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[coding_task, review_task],
            agents=[
                self._agent("a-multi", "coding,code-review"),
                self._agent("a-multi2", "coding,code-review"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        assigned_tasks = {a.task_id for a in actions}
        assert assigned_tasks == {"t-coding", "t-review"}

    def test_multi_type_with_spaces_in_list(self):
        """(e) Whitespace around commas in agent_type is trimmed."""
        task = make_task(id="t-1", agent_type="code-review")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding , code-review , qa")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"

    def test_multi_type_agent_vs_single_type_routing(self):
        """(e) Multi-type and single-type agents coexist; tasks route correctly."""
        review_task = make_task(id="t-review", agent_type="code-review", priority=100)
        qa_task = make_task(id="t-qa", agent_type="qa", priority=100)
        state = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[review_task, qa_task],
            agents=[
                self._agent("a-multi", "coding,code-review"),
                self._agent("a-qa", "qa"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        by_agent = {a.agent_id: a for a in actions}
        assert by_agent["a-multi"].task_id == "t-review"
        assert by_agent["a-qa"].task_id == "t-qa"

    # ── (f) Type mismatch logging ───────────────────────────────────────

    def test_type_mismatch_logged(self, caplog):
        """(f) Type mismatch rejection is logged with task_id, required_type, and agent_type."""
        task = make_task(id="t-review-42", agent_type="code-review")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-coder-7", "coding")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        with caplog.at_level(logging.DEBUG, logger="src.scheduler"):
            actions = Scheduler.schedule(state)
        assert len(actions) == 0
        # Verify the log contains the required debugging fields
        assert "t-review-42" in caplog.text  # task_id
        assert "code-review" in caplog.text  # required_type
        assert "coding" in caplog.text  # agent_type

    def test_type_match_not_logged(self, caplog):
        """(f) Successful type matches do not produce mismatch log entries."""
        task = make_task(id="t-1", agent_type="coding")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        with caplog.at_level(logging.DEBUG, logger="src.scheduler"):
            actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert "mismatch" not in caplog.text.lower()

    def test_no_agent_type_not_logged(self, caplog):
        """(f) Tasks without agent_type do not produce mismatch log entries."""
        task = make_task(id="t-1")  # agent_type=None
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-1", "coding")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        with caplog.at_level(logging.DEBUG, logger="src.scheduler"):
            actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert "mismatch" not in caplog.text.lower()

    def test_multi_type_mismatch_logged_with_full_type_string(self, caplog):
        """(f) When a multi-type agent mismatches, the full agent_type string is logged."""
        task = make_task(id="t-qa-99", agent_type="qa")
        state = SchedulerState(
            projects=[make_project()],
            tasks=[task],
            agents=[self._agent("a-dev-5", "coding,code-review")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
        )
        with caplog.at_level(logging.DEBUG, logger="src.scheduler"):
            actions = Scheduler.schedule(state)
        assert len(actions) == 0
        assert "t-qa-99" in caplog.text
        assert "qa" in caplog.text
        assert "coding,code-review" in caplog.text


class TestAgentAffinity:
    """Tests for agent affinity: prefer idle affinity agent, bounded wait, fallback."""

    # ── Tier 0: Prefer idle affinity agent ────────────────────────────

    def test_idle_affinity_agent_gets_task(self):
        """When the preferred agent is idle, assign the task to it (tier 0)."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task(affinity_agent_id="a-1", created_at=now - 10)],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2", name="claude-2"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
        )
        actions = Scheduler.schedule(state)
        # a-1 is idle and preferred → gets the task
        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"
        assert actions[0].task_id == "t-1"

    def test_affinity_task_prioritised_over_normal(self):
        """An affinity match (tier 0) sorts ahead of non-affinity tasks (tier 1)."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(id="t-normal", priority=10),  # higher prio number ← but no affinity
                make_task(id="t-affinity", priority=50, affinity_agent_id="a-1", created_at=now),
            ],
            agents=[make_agent(id="a-1")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
        )
        actions = Scheduler.schedule(state)
        assert actions[0].task_id == "t-affinity"

    # ── Tier 2: Defer to another idle agent ───────────────────────────

    def test_defer_task_when_preferred_idle_agent_is_someone_else(self):
        """When a task prefers another idle agent, a non-preferred idle agent picks
        a different task instead (tier 2 deferred)."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[
                make_task(id="t-for-a2", priority=10, affinity_agent_id="a-2", created_at=now),
                make_task(id="t-normal", priority=50, created_at=now),
            ],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2", name="claude-2"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 2
        by_agent = {a.agent_id: a.task_id for a in actions}
        # a-2 gets its affinity task; a-1 gets the normal task
        assert by_agent["a-2"] == "t-for-a2"
        assert by_agent["a-1"] == "t-normal"

    # ── Tier 3: Bounded wait for busy affinity agent ──────────────────

    def test_bounded_wait_defers_task_when_affinity_agent_busy(self):
        """When the preferred agent is busy and the wait window hasn't expired,
        the task is deferred (not assigned to another agent)."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-affinity",
                    affinity_agent_id="a-busy",
                    created_at=now - 30,  # only 30s ago, within 120s window
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Only affinity task available, still in wait window → no assignment
        assert len(actions) == 0

    def test_bounded_wait_expires_then_fallback(self):
        """When the wait window expires, the task falls back to any idle agent."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-affinity",
                    affinity_agent_id="a-busy",
                    created_at=now - 200,  # 200s ago, past the 120s window
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Wait expired → a-idle picks it up (fallback to tier 1)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"
        assert actions[0].task_id == "t-affinity"

    def test_bounded_wait_non_affinity_tasks_still_assigned(self):
        """Non-affinity tasks are assigned normally even when affinity tasks are deferred."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-affinity",
                    priority=10,
                    affinity_agent_id="a-busy",
                    created_at=now - 30,  # within wait window
                ),
                make_task(id="t-normal", priority=50, created_at=now),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # t-normal is tier 1, t-affinity is tier 3 → normal assigned
        assert len(actions) == 1
        assert actions[0].task_id == "t-normal"

    def test_bounded_wait_zero_disables_waiting(self):
        """When affinity_wait_seconds=0, no bounded wait — fallback immediately."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-affinity",
                    affinity_agent_id="a-busy",
                    created_at=now - 5,  # very recent
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=0,  # disabled
        )
        actions = Scheduler.schedule(state)
        # No wait → immediate fallback
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"

    def test_bounded_wait_no_now_disables_waiting(self):
        """When now=0.0 (not populated), bounded wait is disabled."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-affinity",
                    affinity_agent_id="a-busy",
                    created_at=time.time() - 5,
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=0.0,  # not set
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # now=0 → no time-based wait → immediate fallback
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"

    # ── Starvation prevention ─────────────────────────────────────────

    def test_no_starvation_tier2_only(self):
        """If all tasks prefer another idle agent (tier 2), the current agent
        still picks one up — no starvation."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project(max_agents=1)],
            tasks=[
                make_task(id="t-for-a2", affinity_agent_id="a-2", created_at=now),
            ],
            agents=[
                make_agent(id="a-1"),
                # a-2 is idle but max_agents=1 means only 1 assignment
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
        )
        actions = Scheduler.schedule(state)
        # a-2 isn't in the agent list → falls to tier 1 (unknown agent)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"

    def test_no_starvation_all_prefer_other_idle_agents(self):
        """When all tasks prefer other idle agents, agent still gets work."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[
                make_task(id="t-1", affinity_agent_id="a-2", created_at=now),
                make_task(id="t-2", affinity_agent_id="a-2", created_at=now),
            ],
            agents=[
                make_agent(id="a-1"),
                make_agent(id="a-2", name="claude-2"),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
        )
        actions = Scheduler.schedule(state)
        # a-2 gets one task (tier 0), a-1 gets the remaining task (tier 2 fallback)
        assert len(actions) == 2

    # ── Backward compatibility ────────────────────────────────────────

    def test_backward_compat_no_affinity_fields(self):
        """Tasks without affinity fields work exactly as before."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[make_task()],
            agents=[make_agent()],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            # now and affinity_wait_seconds default to 0.0 and 120.0
        )
        actions = Scheduler.schedule(state)
        assert len(actions) == 1
        assert actions[0].task_id == "t-1"

    def test_backward_compat_now_not_set(self):
        """When now is not populated (legacy callers), affinity still works
        for idle-agent preference (tiers 0 and 2) but bounded wait is disabled."""
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(id="t-1", affinity_agent_id="a-1"),
            ],
            agents=[make_agent(id="a-1")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            # now=0.0 by default
        )
        actions = Scheduler.schedule(state)
        # Tier 0 still works — idle preferred agent gets the task
        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"

    # ── Edge cases ────────────────────────────────────────────────────

    def test_affinity_agent_not_in_agent_list(self):
        """When the affinity agent ID doesn't match any known agent,
        the task is treated as tier 1 (normal)."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-1",
                    affinity_agent_id="a-nonexistent",
                    created_at=now - 10,
                ),
            ],
            agents=[make_agent(id="a-1")],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Unknown agent → tier 1 → assigned normally
        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"

    def test_mixed_affinity_and_normal_tasks(self):
        """Multiple tasks with different affinity states are ordered correctly."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project(max_agents=3)],
            tasks=[
                make_task(
                    id="t-wait", priority=10,
                    affinity_agent_id="a-busy", created_at=now - 30,
                ),
                make_task(id="t-normal", priority=50, created_at=now),
                make_task(
                    id="t-match", priority=50,
                    affinity_agent_id="a-idle", created_at=now,
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # a-idle should get t-match (tier 0), not t-wait (tier 3)
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"
        assert actions[0].task_id == "t-match"

    def test_bounded_wait_with_custom_timeout(self):
        """Custom affinity_wait_seconds is respected."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-1",
                    affinity_agent_id="a-busy",
                    created_at=now - 15,  # 15s ago
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=10,  # 10s window — already expired
        )
        actions = Scheduler.schedule(state)
        # 15s > 10s → wait expired → fallback
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"

    def test_bounded_wait_just_under_threshold(self):
        """Task created just under the threshold is still deferred."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-1",
                    affinity_agent_id="a-busy",
                    created_at=now - 119,  # 119s, threshold is 120s
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # 119s < 120s → still waiting
        assert len(actions) == 0

    def test_bounded_wait_exactly_at_threshold(self):
        """Task created exactly at the threshold falls back."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-1",
                    affinity_agent_id="a-busy",
                    created_at=now - 120,  # exactly 120s, threshold is 120s
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # 120s >= 120s → wait expired → fallback
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"

    def test_task_with_zero_created_at_skips_wait(self):
        """When task.created_at is 0 (not set), bounded wait is skipped —
        the task falls through to tier 1 and is assigned immediately."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project()],
            tasks=[
                make_task(
                    id="t-1",
                    affinity_agent_id="a-busy",
                    created_at=0.0,  # not set
                ),
            ],
            agents=[
                make_agent(id="a-idle"),
                make_agent(
                    id="a-busy", name="claude-busy", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # created_at=0 → bounded wait skipped → tier 1 → immediate fallback
        assert len(actions) == 1
        assert actions[0].agent_id == "a-idle"

    # ── Roadmap 7.3.4 cases (c), (e), (f) ─────────────────────────────

    def test_fallback_agent_matches_agent_type(self):
        """(c) When affinity wait expires and task falls back, the fallback
        agent must still match the task's agent_type requirement."""
        now = time.time()
        state = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[
                make_task(
                    id="t-review",
                    agent_type="code-review",
                    affinity_agent_id="a-busy",
                    created_at=now - 200,  # past 120s wait window
                ),
            ],
            agents=[
                # a-busy is the preferred agent (code-review type, but busy)
                Agent(
                    id="a-busy", name="busy-reviewer", agent_type="code-review",
                    state=AgentState.BUSY, current_task_id="t-other",
                ),
                # a-coding is idle but wrong type — should NOT pick up the task
                Agent(id="a-coding", name="coder", agent_type="coding", state=AgentState.IDLE),
                # a-review is idle with correct type — SHOULD pick up the task
                Agent(
                    id="a-review", name="reviewer", agent_type="code-review",
                    state=AgentState.IDLE,
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions = Scheduler.schedule(state)
        # Affinity wait expired → falls to tier 1 → but agent_type filtering
        # still applies → a-coding excluded → a-review picks it up
        assert len(actions) == 1
        assert actions[0].agent_id == "a-review"
        assert actions[0].task_id == "t-review"

    def test_affinity_agent_becomes_idle_within_wait_window(self):
        """(e) Affinity agent that becomes idle within the wait window gets
        the task — not the fallback agent.

        Simulates two scheduler rounds: in round 1 the preferred agent is
        busy so the task is deferred; in round 2 the preferred agent is
        idle and picks up the task (tier 0) while the fallback gets a
        different task."""
        now = time.time()

        # Round 1: affinity agent is busy, task is within wait window → deferred
        state_round1 = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[
                make_task(
                    id="t-affinity",
                    affinity_agent_id="a-pref",
                    created_at=now - 30,  # 30s ago, within 120s window
                ),
            ],
            agents=[
                make_agent(id="a-fallback", name="fallback"),
                make_agent(
                    id="a-pref", name="preferred", state=AgentState.BUSY,
                    current_task_id="t-other",
                ),
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now,
            affinity_wait_seconds=120,
        )
        actions_r1 = Scheduler.schedule(state_round1)
        # Only affinity task exists, still in wait window → no assignment
        assert len(actions_r1) == 0

        # Round 2: 10 seconds later, affinity agent has finished its work
        # and become idle.  A second task is also available now.
        now2 = now + 10
        state_round2 = SchedulerState(
            projects=[make_project(max_agents=2)],
            tasks=[
                make_task(
                    id="t-affinity",
                    affinity_agent_id="a-pref",
                    created_at=now - 30,  # still within 120s window
                ),
                make_task(id="t-normal", priority=50),
            ],
            agents=[
                make_agent(id="a-fallback", name="fallback"),
                make_agent(id="a-pref", name="preferred"),  # now IDLE
            ],
            project_token_usage={},
            project_active_agent_counts={},
            tasks_completed_in_window={},
            now=now2,
            affinity_wait_seconds=120,
        )
        actions_r2 = Scheduler.schedule(state_round2)
        # Both agents idle, both tasks available:
        # a-pref gets t-affinity (tier 0), a-fallback gets t-normal
        assert len(actions_r2) == 2
        by_agent = {a.agent_id: a.task_id for a in actions_r2}
        assert by_agent["a-pref"] == "t-affinity"
        assert by_agent["a-fallback"] == "t-normal"

    def test_affinity_reason_logged(self, caplog):
        """(f) affinity_reason is logged for debugging when a task with
        affinity is assigned."""
        now = time.time()
        with caplog.at_level(logging.DEBUG, logger="src.scheduler"):
            state = SchedulerState(
                projects=[make_project()],
                tasks=[
                    make_task(
                        id="t-branch",
                        affinity_agent_id="a-1",
                        affinity_reason="context",
                        created_at=now - 10,
                    ),
                ],
                agents=[make_agent(id="a-1")],
                project_token_usage={},
                project_active_agent_counts={},
                tasks_completed_in_window={},
                now=now,
            )
            actions = Scheduler.schedule(state)

        assert len(actions) == 1
        assert actions[0].agent_id == "a-1"
        # Verify affinity_reason appears in debug logs
        assert "context" in caplog.text
        assert "t-branch" in caplog.text
        assert "a-1" in caplog.text
