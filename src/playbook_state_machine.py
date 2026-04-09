"""Formal PlaybookRun state machine definition.

This module defines the authoritative set of valid playbook run state
transitions and provides utilities for validation.  It is the source of truth
for which (PlaybookRunStatus, PlaybookRunEvent) pairs are legal moves in the
playbook run lifecycle.

The state machine is modelled after the task state machine
(:mod:`src.state_machine`) but is simpler — playbook runs have five states
and seven events.

State diagram::

    ┌─────────┐  TERMINAL_REACHED   ┌───────────┐
    │         ├─────────────────────►│ COMPLETED │
    │         │                      └───────────┘
    │         │  NODE_FAILED /
    │         │  TRANSITION_FAILED / ┌────────┐
    │ RUNNING │  GRAPH_ERROR        │ FAILED │
    │         ├─────────────────────►│        │
    │         │                      └────────┘
    │         │  BUDGET_EXCEEDED     ┌───────────┐
    │         ├─────────────────────►│ TIMED_OUT │
    │         │                      └───────────┘
    │         │  HUMAN_WAIT          ┌────────┐
    │         ├─────────────────────►│ PAUSED │
    └────▲────┘                      │        │
         │      HUMAN_RESUMED        │        │
         └───────────────────────────┤        │
                                     └────────┘

See docs/specs/design/playbooks.md §6 for the execution model specification.
"""

from __future__ import annotations

import logging

from src.models import PlaybookRunEvent, PlaybookRunStatus

logger = logging.getLogger(__name__)


class InvalidPlaybookRunTransition(Exception):
    """Raised when a (status, event) pair has no defined target state."""

    def __init__(self, state: PlaybookRunStatus, event: PlaybookRunEvent):
        self.state = state
        self.event = event
        super().__init__(f"Invalid playbook run transition: ({state.value}, {event.value})")


# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

VALID_PLAYBOOK_RUN_TRANSITIONS: dict[
    tuple[PlaybookRunStatus, PlaybookRunEvent], PlaybookRunStatus
] = {
    # --- Running → terminal states ---
    (PlaybookRunStatus.RUNNING, PlaybookRunEvent.TERMINAL_REACHED): PlaybookRunStatus.COMPLETED,
    (PlaybookRunStatus.RUNNING, PlaybookRunEvent.NODE_FAILED): PlaybookRunStatus.FAILED,
    (PlaybookRunStatus.RUNNING, PlaybookRunEvent.TRANSITION_FAILED): PlaybookRunStatus.FAILED,
    (PlaybookRunStatus.RUNNING, PlaybookRunEvent.GRAPH_ERROR): PlaybookRunStatus.FAILED,
    (PlaybookRunStatus.RUNNING, PlaybookRunEvent.BUDGET_EXCEEDED): PlaybookRunStatus.TIMED_OUT,
    # --- Running → paused (human-in-the-loop) ---
    (PlaybookRunStatus.RUNNING, PlaybookRunEvent.HUMAN_WAIT): PlaybookRunStatus.PAUSED,
    # --- Paused → running (resume) ---
    (PlaybookRunStatus.PAUSED, PlaybookRunEvent.HUMAN_RESUMED): PlaybookRunStatus.RUNNING,
}

# Derived set of valid (from_status, to_status) pairs for quick validation
# without requiring a specific event.
VALID_PLAYBOOK_RUN_STATUS_TRANSITIONS: set[tuple[PlaybookRunStatus, PlaybookRunStatus]] = {
    (from_status, to_status)
    for (from_status, _event), to_status in VALID_PLAYBOOK_RUN_TRANSITIONS.items()
}

# Terminal states — once a run reaches one of these, no further transitions
# are valid (the run is done).
TERMINAL_STATUSES: frozenset[PlaybookRunStatus] = frozenset(
    {
        PlaybookRunStatus.COMPLETED,
        PlaybookRunStatus.FAILED,
        PlaybookRunStatus.TIMED_OUT,
    }
)


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def playbook_run_transition(
    current: PlaybookRunStatus,
    event: PlaybookRunEvent,
) -> PlaybookRunStatus:
    """Look up the target status for a given (current_status, event) pair.

    Raises :class:`InvalidPlaybookRunTransition` if no such transition is
    defined in the table.
    """
    key = (current, event)
    if key not in VALID_PLAYBOOK_RUN_TRANSITIONS:
        raise InvalidPlaybookRunTransition(current, event)
    return VALID_PLAYBOOK_RUN_TRANSITIONS[key]


def is_valid_playbook_run_transition(
    from_status: PlaybookRunStatus,
    to_status: PlaybookRunStatus,
) -> bool:
    """Return *True* if transitioning *from_status* → *to_status* is covered
    by at least one event in the state machine."""
    return (from_status, to_status) in VALID_PLAYBOOK_RUN_STATUS_TRANSITIONS


def is_terminal(status: PlaybookRunStatus) -> bool:
    """Return *True* if *status* is a terminal (final) state."""
    return status in TERMINAL_STATUSES


def validate_transition(
    current: PlaybookRunStatus,
    event: PlaybookRunEvent,
    run_id: str = "<unknown>",
) -> PlaybookRunStatus:
    """Validate and log a playbook run transition.

    Returns the target status on success.  On invalid transitions, logs a
    warning and raises :class:`InvalidPlaybookRunTransition`.  This is the
    primary entry point used by :class:`~src.playbook_runner.PlaybookRunner`.
    """
    try:
        target = playbook_run_transition(current, event)
    except InvalidPlaybookRunTransition:
        logger.warning(
            "Invalid playbook run transition: run=%s current=%s event=%s",
            run_id,
            current.value,
            event.value,
        )
        raise
    logger.debug(
        "Playbook run transition: run=%s %s -[%s]-> %s",
        run_id,
        current.value,
        event.value,
        target.value,
    )
    return target
