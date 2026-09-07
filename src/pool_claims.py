"""Shared predicates for the lifecycle of a pool-held task claim."""

from __future__ import annotations

from src.models import TaskStatus


LIVE_POOL_CLAIM_TASK_STATUSES = frozenset((TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED))


def is_live_pool_claim_task_status(status: TaskStatus | None) -> bool:
    """Whether ``status`` may continue to occupy a pool worker's claim slot."""
    return status in LIVE_POOL_CLAIM_TASK_STATUSES
