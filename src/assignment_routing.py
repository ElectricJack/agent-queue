"""The effective assignment route: the task row itself.

Routing policy lives in the project's bound router, the
``default-assignment-routing`` playbook by default (mandatory-routing spec
2026-09-28).  The orchestrator only needs one fact per task — which
intelligence class it must run under — and that fact is
``tasks.intelligence_class``, written with the route by ``task_route_apply``.
There is no separate decision record to keep fresh: a task without a
claimable route has none here (:func:`claimable_routes`), and the cascade
emits ``task.route_needed`` until the router gives it one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.models import Task
from src.providers.intent import PINNED, effective_intent
from src.routing.sources import DEFAULT_ROUTER_PLAYBOOK_ID

DEFAULT_ASSIGNMENT_PLAYBOOK_ID = DEFAULT_ROUTER_PLAYBOOK_ID


@dataclass(frozen=True)
class EffectiveAssignmentRoute:
    """The one route consumed by schedulers, claims, and launch checks."""

    task_id: str
    intelligence_class: str
    provider: str | None
    source: str
    input_hash: str | None = None
    decision_id: str | None = None


def explicit_route(task: Task, *, vendor: str | None = None) -> EffectiveAssignmentRoute | None:
    """The task's own class, or ``None`` when the playbook has not routed it yet.

    ``provider`` is the pinned profile's vendor **only when the task is
    pinned** (provider-failover D9): that arms the ``required_provider``
    check in ``routing_mismatch`` / ``_check_agent_routing``, so a generic
    worker of the same class on another provider cannot take a pinned task
    on the push path.  ``preferred`` and ``class_only`` leave it ``None``.
    """

    explicit = (task.intelligence_class or "").strip()
    if not explicit:
        return None
    provider = (vendor or None) if effective_intent(task) == PINNED else None
    return EffectiveAssignmentRoute(task.id, explicit, provider, "explicit")


def explicit_routes(
    tasks: Sequence[Task], vendors: Mapping[str, str] | None = None
) -> dict[str, EffectiveAssignmentRoute]:
    """Routes for *tasks*; *vendors* maps a profile id to its harness's vendor."""
    routes: dict[str, EffectiveAssignmentRoute] = {}
    for task in tasks:
        vendor = (vendors or {}).get(task.profile_id or "")
        route = explicit_route(task, vendor=vendor)
        if route is not None:
            routes[task.id] = route
    return routes


def profile_vendor(profile: Any, harness_registry: Any = None, project_id: str = "") -> str:
    """The vendor a profile's harness draws on -- the value ``task_agent_mismatch``
    compares ``required_provider`` against (``harness.provider`` or the inferred one)."""
    from src.agents.routing import _harness
    from src.sessions.spec import _infer_provider_from_harness

    harness_id = str(getattr(profile, "harness", "") or "").strip()
    if not harness_id:
        return ""
    harness = _harness(harness_id, project_id, harness_registry)
    return str(getattr(harness, "provider", "") or "") or _infer_provider_from_harness(harness)


def claimable_routes(
    routes: Mapping[str, EffectiveAssignmentRoute],
    tasks: Sequence[Task],
    ready_projects: frozenset[str] | set[str],
) -> dict[str, EffectiveAssignmentRoute]:
    """*routes* less every queued task whose route is not claimable (routing §9.1, I3).

    A DEFINED, READY or BLOCKED task keeps its route only when the router, an
    override or a role wrote it -- or a legacy route while its project's
    router is not ready (*ready_projects*).  In-flight work keeps its route,
    legacy included, and finishes where it is (§11).  The push scheduler and
    the reconciler read the result, so neither plans for unrouted work.
    """
    from src.models import TaskStatus
    from src.routing.sources import route_is_claimable

    queued = {TaskStatus.DEFINED, TaskStatus.READY, TaskStatus.BLOCKED}
    by_id = {task.id: task for task in tasks}
    kept: dict[str, EffectiveAssignmentRoute] = {}
    for task_id, route in routes.items():
        task = by_id.get(task_id)
        if task is not None and task.status in queued and not route_is_claimable(
            task.profile_id,
            getattr(task, "route_source", None),
            router_ready=task.project_id in ready_projects,
        ):
            continue
        kept[task_id] = route
    return kept


class ExplicitRouting:
    """The orchestrator's routing seam: reads the task row, decides nothing.

    Kept as an object so tests can still swap in a stub (``routes_for``) the
    way they did for the retired LLM coordinator.

    ``db_getter`` / ``harness_registry_getter`` are optional: with them, a
    pinned task's route carries its profile's vendor (one profile read per
    batch that contains a pin); without them every route's provider is
    ``None`` and pins do not bite on the push path.
    """

    def __init__(
        self,
        *,
        db_getter: Callable[[], Any] | None = None,
        harness_registry_getter: Callable[[], Any] | None = None,
    ) -> None:
        self._db_getter = db_getter
        self._harness_registry_getter = harness_registry_getter

    async def _pinned_vendors(self, tasks: Sequence[Task]) -> dict[str, str]:
        pinned = {t.profile_id for t in tasks if effective_intent(t) == PINNED and t.profile_id}
        if not pinned or self._db_getter is None:
            return {}
        db = self._db_getter()
        if db is None:
            return {}
        registry = self._harness_registry_getter() if self._harness_registry_getter else None
        vendors: dict[str, str] = {}
        for profile in await db.list_profiles():
            if profile.id in pinned:
                vendors[profile.id] = profile_vendor(profile, registry)
        return vendors

    async def routes_for(self, tasks: Sequence[Task]) -> dict[str, EffectiveAssignmentRoute]:
        return explicit_routes(tasks, await self._pinned_vendors(tasks))
