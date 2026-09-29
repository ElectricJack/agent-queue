"""Test-only assignment-routing doubles for suites that do not test routing."""

from src.assignment_routing import EffectiveAssignmentRoute


class AlreadyRouted:
    """Treat each supplied task as having passed assignment-route selection."""

    async def reconcile(self) -> None:
        """Keep the cycle's routing sweep out of execution-only tests."""

        return None

    async def routes_for(self, tasks):
        return {
            task.id: EffectiveAssignmentRoute(
                task.id,
                task.intelligence_class,
                None,
                "test",
                "test",
                "test",
            )
            for task in tasks
        }


def install_already_routed(orchestrator) -> None:
    """Keep unrelated execution tests focused on their declared behavior."""

    orchestrator.assignment_routing = AlreadyRouted()


def route_source_for(profile_id: str | None) -> str:
    """The ``route_source`` a test task naming *profile_id* declares.

    Mandatory routing ended the query layer's transitional stamp
    (``ck_tasks_route_source_profile``, spec §9.2): a profile write names its
    source.  A test that only needs "a task routed to this profile" declares
    what the stamp used to write: ``role`` for a stage profile, ``legacy``
    for any other, ``unrouted`` without a profile.
    """
    from src.routing.sources import LEGACY, ROLE, ROLE_PROFILE_IDS, UNROUTED

    if not profile_id:
        return UNROUTED
    return ROLE if profile_id in ROLE_PROFILE_IDS else LEGACY
