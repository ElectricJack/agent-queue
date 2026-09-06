"""The effective assignment route: the task row itself.

Routing policy lives in the ``default-assignment-routing`` playbook (spec:
``docs/superpowers/specs/2026-09-06-assignment-routing-as-playbook.md``).
The orchestrator only needs one fact per task — which intelligence class it
must run under — and that fact is ``tasks.intelligence_class``, written by
``task_route`` at the end of a playbook run or by an operator.  There is no
separate decision record to keep fresh: a task without a class has no route,
and the cascade emits ``task.route_needed`` until the playbook gives it one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from src.models import Task

DEFAULT_ASSIGNMENT_PLAYBOOK_ID = "default-assignment-routing"


@dataclass(frozen=True)
class EffectiveAssignmentRoute:
    """The one route consumed by schedulers, claims, and launch checks."""

    task_id: str
    intelligence_class: str
    provider: str | None
    source: str
    input_hash: str | None = None
    decision_id: str | None = None


def explicit_route(task: Task) -> EffectiveAssignmentRoute | None:
    """The task's own class, or ``None`` when the playbook has not routed it yet."""

    explicit = (task.intelligence_class or "").strip()
    if not explicit:
        return None
    return EffectiveAssignmentRoute(task.id, explicit, None, "explicit")


def explicit_routes(tasks: Sequence[Task]) -> dict[str, EffectiveAssignmentRoute]:
    routes: dict[str, EffectiveAssignmentRoute] = {}
    for task in tasks:
        route = explicit_route(task)
        if route is not None:
            routes[task.id] = route
    return routes


class ExplicitRouting:
    """The orchestrator's routing seam: reads the task row, decides nothing.

    Kept as an object so tests can still swap in a stub (``routes_for``) the
    way they did for the retired LLM coordinator.
    """

    async def routes_for(self, tasks: Sequence[Task]) -> dict[str, EffectiveAssignmentRoute]:
        return explicit_routes(tasks)
