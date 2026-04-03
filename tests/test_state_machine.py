import pytest
from src.models import TaskStatus, TaskEvent
from src.state_machine import (
    task_transition,
    InvalidTransition,
    VALID_TASK_TRANSITIONS,
)

ALL_STATUSES = list(TaskStatus)
ALL_EVENTS = list(TaskEvent)


class TestValidTransitions:
    @pytest.mark.parametrize(
        "state,event,expected",
        [
            (TaskStatus.DEFINED, TaskEvent.DEPS_MET, TaskStatus.READY),
            (TaskStatus.READY, TaskEvent.ASSIGNED, TaskStatus.ASSIGNED),
            (TaskStatus.ASSIGNED, TaskEvent.AGENT_STARTED, TaskStatus.IN_PROGRESS),
            (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_COMPLETED, TaskStatus.COMPLETED),
            (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_FAILED, TaskStatus.FAILED),
            (TaskStatus.IN_PROGRESS, TaskEvent.TOKENS_EXHAUSTED, TaskStatus.PAUSED),
            (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_QUESTION, TaskStatus.WAITING_INPUT),
            (TaskStatus.WAITING_INPUT, TaskEvent.HUMAN_REPLIED, TaskStatus.IN_PROGRESS),
            (TaskStatus.WAITING_INPUT, TaskEvent.INPUT_TIMEOUT, TaskStatus.PAUSED),
            (TaskStatus.PAUSED, TaskEvent.RESUME_TIMER, TaskStatus.READY),
            (TaskStatus.FAILED, TaskEvent.RETRY, TaskStatus.READY),
            (TaskStatus.FAILED, TaskEvent.MAX_RETRIES, TaskStatus.BLOCKED),
        ],
    )
    def test_valid_transition(self, state, event, expected):
        result = task_transition(state, event)
        assert result == expected


class TestInvalidTransitions:
    @pytest.mark.parametrize(
        "state,event",
        [(s, e) for s in ALL_STATUSES for e in ALL_EVENTS if (s, e) not in VALID_TASK_TRANSITIONS],
    )
    def test_invalid_transition_rejected(self, state, event):
        with pytest.raises(InvalidTransition):
            task_transition(state, event)


class TestTransitionTableCompleteness:
    def test_every_state_has_at_least_one_outgoing_transition(self):
        """Non-terminal states must have at least one valid transition."""
        terminal = {TaskStatus.COMPLETED, TaskStatus.BLOCKED}
        for state in ALL_STATUSES:
            if state in terminal:
                continue
            outgoing = [e for e in ALL_EVENTS if (state, e) in VALID_TASK_TRANSITIONS]
            assert len(outgoing) > 0, f"{state} has no outgoing transitions"

    def test_terminal_states_have_only_admin_outgoing_transitions(self):
        """Terminal states should only have admin/recovery outgoing transitions."""
        terminal = {TaskStatus.COMPLETED, TaskStatus.BLOCKED}
        admin_events = {
            TaskEvent.ADMIN_SKIP,
            TaskEvent.ADMIN_STOP,
            TaskEvent.ADMIN_RESTART,
        }
        for state in terminal:
            outgoing = [e for e in ALL_EVENTS if (state, e) in VALID_TASK_TRANSITIONS]
            non_admin = [e for e in outgoing if e not in admin_events]
            assert len(non_admin) == 0, (
                f"Terminal {state} has non-admin outgoing transitions: {non_admin}"
            )

    def test_paused_always_leads_to_ready(self):
        """PAUSED must always have a path back to READY (deadlock prevention)."""
        result = task_transition(TaskStatus.PAUSED, TaskEvent.RESUME_TIMER)
        assert result == TaskStatus.READY


from src.state_machine import validate_dag, validate_dag_with_new_edge, CyclicDependencyError


class TestDAGValidation:
    def test_no_dependencies(self):
        deps = {}
        validate_dag(deps)  # should not raise

    def test_linear_chain(self):
        deps = {"t-2": {"t-1"}, "t-3": {"t-2"}}
        validate_dag(deps)  # should not raise

    def test_diamond_dependency(self):
        deps = {"t-3": {"t-1", "t-2"}, "t-4": {"t-3"}}
        validate_dag(deps)  # should not raise

    def test_self_dependency_rejected(self):
        deps = {"t-1": {"t-1"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_two_node_cycle_rejected(self):
        deps = {"t-1": {"t-2"}, "t-2": {"t-1"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_three_node_cycle_rejected(self):
        deps = {"t-1": {"t-2"}, "t-2": {"t-3"}, "t-3": {"t-1"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_cycle_in_larger_graph_rejected(self):
        deps = {
            "t-2": {"t-4"},
            "t-3": {"t-2"},
            "t-4": {"t-3"},
        }
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_add_dependency_validates(self):
        """Adding a dependency that would create a cycle is rejected."""
        existing = {"t-2": {"t-1"}, "t-3": {"t-2"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag_with_new_edge(existing, "t-1", depends_on="t-3")
