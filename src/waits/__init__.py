"""Verified wait primitives; importing this package never activates v2.

Transaction lock order: tasks -> wait_records -> continuations ->
admission_reservations -> wait_deadlines -> wait_edges. Registration takes
the semantic-graph advisory lock before row locks. Adapters use the caller's
connection; only CommandHandler callers may commit mutations.
"""

from src.waits.conditions import Condition, Observation, Scope, SourceKey, WaitError
from src.waits.registry import ConditionRegistry, build_registry

__all__ = [
    "Condition",
    "ConditionRegistry",
    "Observation",
    "Scope",
    "SourceKey",
    "WaitError",
    "build_registry",
]
