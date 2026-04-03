"""Formal task state machine definition and dependency graph validation.

This module defines the authoritative set of valid task state transitions and
provides utilities for DAG (directed acyclic graph) validation of task
dependencies. It is the source of truth for which (TaskStatus, TaskEvent) pairs
are legal moves in the task lifecycle.

IMPORTANT: As of now, this state machine is used only for validation logging
and lookups — the orchestrator does NOT enforce transitions through this module.
All status changes go directly through db.update_task(). This means invalid
transitions can occur in practice if the orchestrator has bugs. Enforcing
transitions is a planned improvement.

See specs/models-and-state-machine.md for the full behavioral specification.
"""

from __future__ import annotations

from src.models import TaskStatus, TaskEvent


class InvalidTransition(Exception):
    def __init__(self, state: TaskStatus, event: TaskEvent):
        self.state = state
        self.event = event
        super().__init__(f"Invalid transition: ({state.value}, {event.value})")


VALID_TASK_TRANSITIONS: dict[tuple[TaskStatus, TaskEvent], TaskStatus] = {
    # This table is organized into groups:
    #   1. Core lifecycle — the happy path from DEFINED through COMPLETED
    #   2. Direct shortcuts — skip intermediate FAILED state for retry/block
    #   3. Administrative operations — manual overrides (skip, stop, restart)
    #   4. PR lifecycle — PR closed without merge
    #   5. Error/timeout — agent crashes, timeouts
    #   6. Daemon recovery — requeue tasks that were in-flight when the daemon restarted
    #
    # Each entry maps (current_status, event) -> new_status.
    # --- Core lifecycle ---
    (TaskStatus.DEFINED, TaskEvent.DEPS_MET): TaskStatus.READY,
    (TaskStatus.READY, TaskEvent.ASSIGNED): TaskStatus.ASSIGNED,
    (TaskStatus.ASSIGNED, TaskEvent.AGENT_STARTED): TaskStatus.IN_PROGRESS,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_COMPLETED): TaskStatus.COMPLETED,
    (TaskStatus.IN_PROGRESS, TaskEvent.PR_CREATED): TaskStatus.AWAITING_APPROVAL,
    (TaskStatus.IN_PROGRESS, TaskEvent.MERGE_FAILED): TaskStatus.BLOCKED,
    (TaskStatus.IN_PROGRESS, TaskEvent.MERGE_SUCCEEDED): TaskStatus.COMPLETED,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_FAILED): TaskStatus.FAILED,
    (TaskStatus.IN_PROGRESS, TaskEvent.TOKENS_EXHAUSTED): TaskStatus.PAUSED,
    (TaskStatus.IN_PROGRESS, TaskEvent.AGENT_QUESTION): TaskStatus.WAITING_INPUT,
    (TaskStatus.WAITING_INPUT, TaskEvent.HUMAN_REPLIED): TaskStatus.IN_PROGRESS,
    (TaskStatus.WAITING_INPUT, TaskEvent.INPUT_TIMEOUT): TaskStatus.PAUSED,
    (TaskStatus.PAUSED, TaskEvent.RESUME_TIMER): TaskStatus.READY,
    (TaskStatus.AWAITING_APPROVAL, TaskEvent.PR_MERGED): TaskStatus.COMPLETED,
    # --- Plan approval lifecycle ---
    (TaskStatus.IN_PROGRESS, TaskEvent.PLAN_FOUND): TaskStatus.AWAITING_PLAN_APPROVAL,
    (
        TaskStatus.READY,
        TaskEvent.PLAN_FOUND,
    ): TaskStatus.AWAITING_PLAN_APPROVAL,  # manual /process-plan
    (TaskStatus.AWAITING_PLAN_APPROVAL, TaskEvent.PLAN_APPROVED): TaskStatus.COMPLETED,
    (TaskStatus.AWAITING_PLAN_APPROVAL, TaskEvent.PLAN_REJECTED): TaskStatus.READY,
    (TaskStatus.AWAITING_PLAN_APPROVAL, TaskEvent.PLAN_DELETED): TaskStatus.COMPLETED,
    (TaskStatus.FAILED, TaskEvent.RETRY): TaskStatus.READY,
    (TaskStatus.FAILED, TaskEvent.MAX_RETRIES): TaskStatus.BLOCKED,
    # --- Direct shortcuts (skip intermediate FAILED state) ---
    (TaskStatus.IN_PROGRESS, TaskEvent.MAX_RETRIES): TaskStatus.BLOCKED,
    (TaskStatus.IN_PROGRESS, TaskEvent.RETRY): TaskStatus.READY,
    # --- Administrative operations ---
    (TaskStatus.BLOCKED, TaskEvent.ADMIN_SKIP): TaskStatus.COMPLETED,
    (TaskStatus.FAILED, TaskEvent.ADMIN_SKIP): TaskStatus.COMPLETED,
    (TaskStatus.IN_PROGRESS, TaskEvent.ADMIN_STOP): TaskStatus.BLOCKED,
    # Admin restart — from any non-IN_PROGRESS state
    (TaskStatus.BLOCKED, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.FAILED, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.COMPLETED, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.PAUSED, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.DEFINED, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.ASSIGNED, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.AWAITING_APPROVAL, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.AWAITING_PLAN_APPROVAL, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    (TaskStatus.WAITING_INPUT, TaskEvent.ADMIN_RESTART): TaskStatus.READY,
    # --- PR lifecycle ---
    (TaskStatus.AWAITING_APPROVAL, TaskEvent.PR_CLOSED): TaskStatus.BLOCKED,
    # --- Error / timeout ---
    (TaskStatus.IN_PROGRESS, TaskEvent.TIMEOUT): TaskStatus.BLOCKED,
    (TaskStatus.ASSIGNED, TaskEvent.TIMEOUT): TaskStatus.BLOCKED,
    (TaskStatus.ASSIGNED, TaskEvent.EXECUTION_ERROR): TaskStatus.READY,
    # --- Daemon recovery ---
    (TaskStatus.IN_PROGRESS, TaskEvent.RECOVERY): TaskStatus.READY,
    (TaskStatus.ASSIGNED, TaskEvent.RECOVERY): TaskStatus.READY,
}

# Derived set of valid (from_status, to_status) pairs for quick validation
# without requiring a specific event.
VALID_STATUS_TRANSITIONS: set[tuple[TaskStatus, TaskStatus]] = {
    (from_status, to_status) for (from_status, _event), to_status in VALID_TASK_TRANSITIONS.items()
}


def task_transition(current: TaskStatus, event: TaskEvent) -> TaskStatus:
    """Look up the target status for a given (current_status, event) pair.

    Raises ``InvalidTransition`` if no such transition is defined.
    """
    key = (current, event)
    if key not in VALID_TASK_TRANSITIONS:
        raise InvalidTransition(current, event)
    return VALID_TASK_TRANSITIONS[key]


def is_valid_status_transition(from_status: TaskStatus, to_status: TaskStatus) -> bool:
    """Return *True* if transitioning from *from_status* to *to_status* is
    covered by at least one event in the state machine."""
    return (from_status, to_status) in VALID_STATUS_TRANSITIONS


class CyclicDependencyError(Exception):
    def __init__(self, cycle: list[str] | None = None):
        msg = "Cyclic dependency detected"
        if cycle:
            msg += f": {' -> '.join(cycle)}"
        super().__init__(msg)


def validate_dag(deps: dict[str, set[str]]) -> None:
    """Validate that the task dependency graph contains no cycles.

    Uses a three-color DFS (white/gray/black) to detect back-edges. This is
    called when creating tasks with dependencies and when adding new dependency
    edges to prevent circular chains that would leave tasks stuck in DEFINED
    forever.

    Raises CyclicDependencyError if a cycle is found.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    all_nodes = set(deps.keys())
    for targets in deps.values():
        all_nodes.update(targets)

    color: dict[str, int] = {n: WHITE for n in all_nodes}

    def dfs(node: str) -> None:
        color[node] = GRAY
        for dep in deps.get(node, set()):
            if color[dep] == GRAY:
                raise CyclicDependencyError([node, dep])
            if color[dep] == WHITE:
                dfs(dep)
        color[node] = BLACK

    for node in all_nodes:
        if color[node] == WHITE:
            dfs(node)


def validate_dag_with_new_edge(deps: dict[str, set[str]], task_id: str, depends_on: str) -> None:
    """Check that adding a dependency edge (task_id -> depends_on) won't create a cycle.

    Makes a copy of the dependency graph, adds the proposed edge, and runs
    full DAG validation. Used by the command handler before persisting a new
    dependency to the database.
    """
    new_deps = {k: set(v) for k, v in deps.items()}
    new_deps.setdefault(task_id, set()).add(depends_on)
    validate_dag(new_deps)
