"""Pure system enum catalog shared by the daemon and offline CLI."""

from __future__ import annotations


def get_surface_schema() -> dict:
    """Return the code-owned enum catalog without database or daemon access."""
    from src.commands.session_commands import VALID_OUTCOMES
    from src.database.queries.session_queries import _SESSION_TRANSITIONS
    from src.database.tables import GATE_STATUSES, GATE_TYPES, TASK_DEP_TYPES
    from src.models import AgentState, ClaimResult, CLAIM_PHASES, TaskStatus, TaskType

    return {
        "schema_version": 1,
        "enums": {
            "task_status": [s.value for s in TaskStatus],
            "task_type": [t.value for t in TaskType],
            "dependency_type": list(TASK_DEP_TYPES),
            "gate_type": list(GATE_TYPES),
            "gate_status": list(GATE_STATUSES),
            "hierarchy_error": [
                "not_found",
                "cross_project",
                "cycle",
                "depth",
                "self_parent",
                "container_closed",
                "has_children",
                "open_children",
                "open_descendants",
                "live_descendants",
                "manually_paused_descendants",
                "cycle_check_skipped",
            ],
            "claim_result": [r.value for r in ClaimResult],
            "claim_phase": list(CLAIM_PHASES),
            "lifecycle": ["task", "named", "pool"],
            "session_state": list(_SESSION_TRANSITIONS),
            "agent_state": [s.value for s in AgentState],
            "outcome": list(VALID_OUTCOMES),
        },
    }
