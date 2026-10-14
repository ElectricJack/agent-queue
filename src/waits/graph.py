"""Registration's semantic cycle guard. Priority propagation is a separate writer."""

from collections import defaultdict

from sqlalchemy import select

from src.database.tables import task_dependencies, wait_edges
from src.models import BLOCKING_DEP_TYPES


async def would_create_cycle(conn, proposed) -> bool:
    """Test reachability in dependencies plus active semantic waits, not resource holds.

    Like task_graph.validator._check_cycles, all blocking dependency types
    participate. Check only cycles introduced by proposed edges so unrelated
    historical corruption does not reject every registration in the fleet.
    """
    adjacency = defaultdict(set)
    for waiter, blocker in await conn.execute(
        select(
            task_dependencies.c.task_id,
            task_dependencies.c.depends_on_task_id,
        ).where(task_dependencies.c.dep_type.in_(sorted(BLOCKING_DEP_TYPES)))
    ):
        adjacency[f"task:{waiter}"].add(f"task:{blocker}")
    for waiter, blocker in await conn.execute(
        select(
            wait_edges.c.waiter_key,
            wait_edges.c.blocker_key,
        ).where(wait_edges.c.active.is_(True), wait_edges.c.relation == "semantic")
    ):
        adjacency[waiter].add(blocker)
    semantic = [e for e in proposed if e.relation == "semantic"]
    for edge in semantic:
        adjacency[edge.waiter.key].add(edge.blocker.key)
    for edge in semantic:
        visited = set()
        frontier = [edge.blocker.key]
        while frontier:
            node = frontier.pop()
            if node == edge.waiter.key:
                return True
            if node not in visited:
                visited.add(node)
                frontier.extend(adjacency[node] - visited)
    return False
