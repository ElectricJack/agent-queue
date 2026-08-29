import pytest
from src.models import TaskStatus, TaskEvent
from src.state_machine import (
    CyclicDependencyError,
    task_transition,
    validate_dag,
    validate_dag_with_new_edge,
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

    def test_five_thousand_long_chain_does_not_recurse(self):
        """A long chain is ordinary data at spec §15.2 scale — the walk is
        iterative, so it must not hit Python's recursion limit."""
        deps = {f"t-{i + 1}": {f"t-{i}"} for i in range(1, 5000)}
        validate_dag(deps)  # should not raise (RecursionError included)

    def test_cycle_at_the_end_of_a_long_chain_is_still_found(self):
        deps = {f"t-{i + 1}": {f"t-{i}"} for i in range(1, 5000)}
        deps["t-1"] = {"t-4999"}
        with pytest.raises(CyclicDependencyError):
            validate_dag(deps)

    def test_add_dependency_validates(self):
        """Adding a dependency that would create a cycle is rejected."""
        existing = {"t-2": {"t-1"}, "t-3": {"t-2"}}
        with pytest.raises(CyclicDependencyError):
            validate_dag_with_new_edge(existing, "t-1", depends_on="t-3")


# ---------------------------------------------------------------------------
# Enforcement-flag contract (trust-and-ops §6 — invariant tests)
#
# The flag itself is consumed by Workstream D's ``transition_task``.  These
# tests pin the *contract* so that landing enforcement cannot quietly change
# the default or the escape hatch.
# ---------------------------------------------------------------------------


class TestEnforcementFlagContract:
    def test_flag_exists_and_defaults_to_warn_only(self):
        from src.config import AppConfig, StateMachineConfig

        assert StateMachineConfig().enforce is False, (
            "state_machine.enforce must default to False — enabling strict mode by "
            "default would turn today's warnings into hard failures on upgrade."
        )
        assert AppConfig().state_machine.enforce is False

    def test_flag_is_parsed_from_config_yaml(self, tmp_path):
        from src.config import load_config

        path = tmp_path / "config.yaml"
        d = tmp_path.as_posix()
        path.write_text(
            f"data_dir: {d}\n"
            f"workspace_dir: {d}/ws\n"
            f"database:\n  url: {d}/aq.db\n"
            "discord:\n  bot_token: t\n  guild_id: '1'\n"
            "state_machine:\n  enforce: true\n",
            encoding="utf-8",
        )
        config = load_config(str(path))
        assert config.state_machine.enforce is True

    def test_flag_validates_cleanly_in_both_positions(self):
        from src.config import StateMachineConfig

        assert StateMachineConfig(enforce=True).validate() == []
        assert StateMachineConfig(enforce=False).validate() == []

    def test_predicate_enforcement_will_consult_is_available(self):
        """``is_valid_status_transition`` is the predicate strict mode gates on.

        Enforcement must reuse this table rather than re-deriving legality, so
        the warn-only path and the strict path can never disagree.
        """
        from src.state_machine import is_valid_status_transition

        assert is_valid_status_transition(TaskStatus.READY, TaskStatus.ASSIGNED) is True
        assert is_valid_status_transition(TaskStatus.COMPLETED, TaskStatus.ASSIGNED) is False

    def test_transition_table_is_the_single_source_of_truth(self):
        """Every entry in the table is reachable through ``task_transition``."""
        for (state, event), expected in VALID_TASK_TRANSITIONS.items():
            assert task_transition(state, event) == expected
