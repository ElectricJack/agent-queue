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

from typing import Iterator

from src.models import BLOCKING_DEP_TYPES, DepType, TaskStatus, TaskEvent

__all__ = [
    "BLOCKING_DEP_TYPES",
    "CyclicDependencyError",
    "InvalidTransition",
    "VALID_STATUS_TRANSITIONS",
    "VALID_TASK_TRANSITIONS",
    "is_valid_status_transition",
    "task_transition",
    "validate_dag",
    "validate_dag_with_new_edge",
    "validate_waits_for",
]


class InvalidTransition(Exception):
    def __init__(
        self,
        state: TaskStatus,
        event: TaskEvent | None = None,
        *,
        from_status: TaskStatus | None = None,
        to_status: TaskStatus | None = None,
    ):
        self.state = state
        self.event = event
        # WG-5: enforcement callers pass ``from_status``/``to_status`` for
        # API error payloads.  ``state`` remains the original constructor
        # for back-compat with existing event-driven callers.
        self.from_status = from_status if from_status is not None else state
        self.to_status = to_status
        if event is not None:
            msg = f"Invalid transition: ({state.value}, {event.value})"
        elif to_status is not None:
            msg = (
                f"Invalid transition: ({self.from_status.value} -> "
                f"{to_status.value})"
            )
        else:
            msg = f"Invalid transition from {state.value}"
        super().__init__(msg)


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
    (TaskStatus.AWAITING_PLAN_APPROVAL, TaskEvent.PLAN_APPROVED): TaskStatus.IN_PROGRESS,
    (TaskStatus.IN_PROGRESS, TaskEvent.SUBTASKS_COMPLETED): TaskStatus.COMPLETED,
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
    # --- Conditional-edge disposal (work-graph design §3.1) ---
    # A contingency task whose `conditional-blocks` dependency COMPLETED can
    # never run; the cascade closes it as a no-op rather than let it rot.
    (TaskStatus.DEFINED, TaskEvent.CONDITIONAL_DEAD): TaskStatus.COMPLETED,
    (TaskStatus.READY, TaskEvent.CONDITIONAL_DEAD): TaskStatus.COMPLETED,
    # Deliberately no BLOCKED -> COMPLETED entry: BLOCKED is terminal and
    # only admin events may leave it.  A contingency task never reaches
    # BLOCKED through its conditional edge anyway (an unsatisfiable edge
    # keeps it DEFINED), and auto-completing a task that was blocked by a
    # failure would erase that failure record.
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

    The walk is **iterative** (an explicit stack of ``(node, iterator)``
    frames): a long dependency chain is ordinary data — a 5,000-task chain at
    spec §15.2 scale would blow Python's recursion limit in a recursive DFS.

    Raises CyclicDependencyError if a cycle is found.
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    all_nodes = set(deps.keys())
    for targets in deps.values():
        all_nodes.update(targets)

    color: dict[str, int] = {n: WHITE for n in all_nodes}
    empty: set[str] = set()

    for root in all_nodes:
        if color[root] != WHITE:
            continue
        color[root] = GRAY
        stack: list[tuple[str, Iterator[str]]] = [(root, iter(deps.get(root, empty)))]
        while stack:
            node, remaining = stack[-1]
            descended = False
            for dep in remaining:
                if color[dep] == GRAY:
                    raise CyclicDependencyError([node, dep])
                if color[dep] == WHITE:
                    color[dep] = GRAY
                    # ``remaining`` is kept in the frame, so this node
                    # resumes exactly where it left off when we come back up.
                    stack.append((dep, iter(deps.get(dep, empty))))
                    descended = True
                    break
            if not descended:
                color[node] = BLACK
                stack.pop()


def validate_dag_with_new_edge(
    deps: dict[str, set[str]],
    task_id: str,
    depends_on: str,
    dep_type: str = DepType.BLOCKS.value,
) -> None:
    """Check that adding a dependency edge (task_id -> depends_on) won't create a cycle.

    Makes a copy of the dependency graph, adds the proposed edge, and runs
    full DAG validation. Used by the command handler before persisting a new
    dependency to the database.

    Acyclicity is enforced over **blocking edges only** (work-graph design
    §11): ``blocks``, ``parent-child``, ``waits-for`` and
    ``conditional-blocks`` all deadlock in a cycle, while
    ``discovered-from`` legitimately points backwards and ``related`` is
    symmetric.  Self-edges are rejected for every type.

    ``deps`` must therefore already be restricted to blocking edges — which
    is what ``get_all_dependencies()`` returns by default.
    """
    if task_id == depends_on:
        raise CyclicDependencyError([task_id, depends_on])
    if dep_type not in BLOCKING_DEP_TYPES:
        return
    new_deps = {k: set(v) for k, v in deps.items()}
    new_deps.setdefault(task_id, set()).add(depends_on)
    validate_dag(new_deps)


def validate_waits_for(
    parent_child_edges: dict[str, set[str]],
    waiter_id: str,
    container_id: str,
) -> None:
    """Reject a ``waits-for`` edge that can never be satisfied.

    A waiter that is itself a (transitive) child of the container fans in
    over a set containing itself — a permanent deadlock the plain DAG check
    cannot see, because the two edges point in opposite directions
    (work-graph design §11).

    ``parent_child_edges`` maps ``child_id -> {container_ids}`` — exactly
    what ``get_parent_child_edges()`` returns.

    Raises :class:`CyclicDependencyError` describing the ancestry path.
    """
    if waiter_id == container_id:
        raise CyclicDependencyError([waiter_id, container_id])

    # Walk the waiter's ancestry; if the container appears, the fan-in set
    # contains the waiter itself.
    seen: set[str] = {waiter_id}
    stack = [(waiter_id, [waiter_id])]
    while stack:
        node, path = stack.pop()
        for parent in sorted(parent_child_edges.get(node, set())):
            if parent == container_id:
                raise CyclicDependencyError([*path, container_id])
            if parent not in seen:
                seen.add(parent)
                stack.append((parent, [*path, parent]))
