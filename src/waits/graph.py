"""Deterministic wait edges and urgency on caller-owned transactions.

All graph writers take GRAPH_LOCK before task/record locks. Traversals are
bounded and refuse before writing if the component exceeds the budget.
Resource cycles terminate; priorities are derived from bases, never stale caches.
"""

import heapq
import time
import uuid
from collections import defaultdict

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import task_dependencies, tasks, wait_edges, wait_priority_projection
from src.models import BLOCKING_DEP_TYPES
from src.waits.conditions import WaitError

GRAPH_LOCK = 0x41515747524150
MAX_COMPONENT_NODES = 10_000


async def lock_graph(conn):
    await conn.execute(select(func.pg_advisory_xact_lock(GRAPH_LOCK)))


async def _walk(conn, roots, *, reverse=False):
    seen = set(roots)
    frontier = set(roots)
    adjacency = defaultdict(set)
    lookup = wait_edges.c.blocker_key if reverse else wait_edges.c.waiter_key
    while frontier:
        if len(seen) > MAX_COMPONENT_NODES:
            raise WaitError("wait.graph_limit", "wait component exceeds the traversal budget")
        rows = await conn.execute(
            select(wait_edges.c.waiter_key, wait_edges.c.blocker_key).where(
                wait_edges.c.active.is_(True), lookup.in_(sorted(frontier))
            )
        )
        following = set()
        for waiter, blocker in rows:
            adjacency[waiter].add(blocker)
            following.add(waiter if reverse else blocker)
        frontier = following - seen
        seen.update(frontier)
    return seen, adjacency


async def lock_component_tasks(conn, changed_keys, *, lock_rows=True):
    """Lock every potentially affected task before touching wait/edge rows.

    For a new edge, pass BOTH endpoints; for clearing, pass its waiter before
    clearing. Registration uses lock_rows=False until subscribe/re-observe:
    locking a producer task earlier would block the very source commit that
    second observation must detect. The graph fence serialises priority writes.
    """
    await lock_graph(conn)
    affected, _ = await _walk(conn, changed_keys)
    ancestors, _ = await _walk(conn, affected, reverse=True)
    ids = sorted(k.removeprefix("task:") for k in ancestors if k.startswith("task:"))
    if ids and lock_rows:
        await conn.execute(
            select(tasks.c.id).where(tasks.c.id.in_(ids)).order_by(tasks.c.id).with_for_update()
        )
    return affected


async def recompute_from(conn, changed_keys, *, now=None):
    """Retract and propagate urgency from current bases through active edges.

    Only the downstream closure is written. Its ancestors supply donors,
    including paths through neutral derived nodes. Stable donor id, then path
    length and lexical path break ties. Unrelated components remain untouched.
    Call lock_component_tasks before any wait row locks in this transaction.
    """
    await lock_graph(conn)
    now = time.time() if now is None else now
    affected, _ = await _walk(conn, changed_keys)
    ancestors, adjacency = await _walk(conn, affected, reverse=True)
    ids = sorted(k.removeprefix("task:") for k in ancestors if k.startswith("task:"))
    rows = (
        (
            await conn.execute(
                select(tasks.c.id, tasks.c.priority, tasks.c.effective_priority)
                .where(tasks.c.id.in_(ids))
                .order_by(tasks.c.id)
            )
        )
        .mappings()
        .all()
    )
    bases = {f"task:{r['id']}": r["priority"] for r in rows}
    best = {}
    queue = []
    for key, priority in sorted(bases.items()):
        candidate = (priority, key, 0, (key,))
        best[key] = candidate
        heapq.heappush(queue, (*candidate, key))
    while queue:
        priority, donor, length, path, node = heapq.heappop(queue)
        if best[node] != (priority, donor, length, path):
            continue
        for blocker in sorted(adjacency[node]):
            candidate = (priority, donor, length + 1, (*path, blocker))
            if blocker not in best or candidate < best[blocker]:
                best[blocker] = candidate
                heapq.heappush(queue, (*candidate, blocker))
    version = (
        await conn.scalar(select(func.max(wait_priority_projection.c.graph_version))) or 0
    ) + 1
    projected = set(
        (
            await conn.execute(
                select(wait_priority_projection.c.task_id).where(
                    wait_priority_projection.c.task_id.in_(ids),
                )
            )
        ).scalars()
    )
    changed = set()
    for r in rows:
        key = f"task:{r['id']}"
        if key not in affected:
            continue
        priority, donor, _, path = best[key]
        if priority == r["priority"]:
            donor, path = None, ()
        if priority != r["effective_priority"]:
            await conn.execute(
                update(tasks)
                .where(tasks.c.id == r["id"])
                .values(
                    effective_priority=priority,
                )
            )
            from src.database.tables import admission_reservations

            await conn.execute(update(admission_reservations).where(
                admission_reservations.c.task_id == r["id"],
                admission_reservations.c.state.in_(("pending", "overdue", "granted")),
            ).values(effective_priority=priority))
            changed.add(r["id"])
        # Base-only tasks need no donor record. In particular, ordinary v1
        # task_set writes do not introduce v2 lifecycle references.
        if donor is None and r["id"] not in projected:
            continue
        values = dict(
            task_id=r["id"],
            donor_task_id=donor.removeprefix("task:") if donor else None,
            donor_path=list(path),
            graph_version=version,
            computed_at=now,
        )
        stmt = pg_insert(wait_priority_projection).values(**values)
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=[wait_priority_projection.c.task_id],
                set_={k: v for k, v in values.items() if k != "task_id"},
            )
        )
    return changed


async def apply_base_priority_change(conn, task_id, *, now=None):
    """After the fenced base write, lower OR raise the affected closure."""
    return await recompute_from(conn, {f"task:{task_id}"}, now=now)


async def add_edges(conn, wait_id, edges, *, source_revision, now):
    """Caller has verified the adapter and locked the component and wait row."""
    if await would_create_cycle(conn, edges):
        raise WaitError("wait.cycle", "condition would create a semantic dependency cycle")
    if edges:
        await conn.execute(
            insert(wait_edges),
            [
                dict(
                    id=str(uuid.uuid4()),
                    wait_id=wait_id,
                    waiter_key=e.waiter.key,
                    waiter_project_id=e.waiter.project_id,
                    blocker_key=e.blocker.key,
                    blocker_project_id=e.blocker.project_id,
                    relation=e.relation,
                    source_revision=source_revision,
                    created_at=now,
                )
                for e in edges
            ],
        )


async def clear_edges(conn, wait_id, *, now):
    """Keep historical evidence and return the old endpoints for retraction."""
    rows = (
        await conn.execute(
            update(wait_edges)
            .where(
                wait_edges.c.wait_id == wait_id,
                wait_edges.c.active.is_(True),
            )
            .values(active=False, cleared_at=now)
            .returning(
                wait_edges.c.waiter_key,
                wait_edges.c.blocker_key,
            )
        )
    ).all()
    return {key for row in rows for key in row}


async def would_create_cycle(conn, proposed, *, excluding_wait_id=None) -> bool:
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
    edge_query = select(
        wait_edges.c.waiter_key,
        wait_edges.c.blocker_key,
    ).where(wait_edges.c.active.is_(True), wait_edges.c.relation == "semantic")
    if excluding_wait_id is not None:
        edge_query = edge_query.where(wait_edges.c.wait_id != excluding_wait_id)
    for waiter, blocker in await conn.execute(edge_query):
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
