"""Test-only assignment-routing doubles for suites that do not test routing."""

from src.assignment_routing import EffectiveAssignmentRoute


class AlreadyRouted:
    """Treat each supplied task as having passed assignment-route selection."""

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
